import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections
from django.test import Client
from test_previews import artifact as saved_artifact  # noqa: F401
from test_previews import bundle, product_factory  # noqa: F401

from tempo import release_configuration
from tempo.acceptance_contract import digest
from tempo_web.models import DeploymentTarget, ReleaseConfiguration
from tempo_web.release_views import evaluate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def configured(saved_artifact, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("TEMPO_RELEASE_ROOT", str(tmp_path / "releases"))
    monkeypatch.setenv("TEMPO_RELEASE_PORT", "8032")
    return saved_artifact


def test_approval_binds_exact_artifact_and_named_environment_without_authorizing_deployment(
    configured,
):
    artifact = configured
    saved = release_configuration.approve(artifact.pk, "mini-app-staging", 0, artifact.user.pk)
    assert saved.digest == digest(saved.specification)
    assert saved.specification["environment"] == "local-staging"
    assert saved.specification["artifact_digest"] == artifact.digest
    assert saved.specification["runtime"] == {
        "kind": "static",
        "variables": {},
        "secret_references": [],
        "migrations": [],
    }
    assert release_configuration.summary(artifact)["passed"]
    report = evaluate(artifact)
    assert {gate["id"]: gate["status"] for gate in report["gates"]}["target"] == "passed"
    assert not report["ready"]
    assert report["release_configuration"]["digest"] == saved.digest
    assert (
        release_configuration.approve(
            artifact.pk, "mini-app-staging", saved.pk, artifact.user.pk
        ).pk
        == saved.pk
    )
    with pytest.raises(ValueError, match="changed"):
        release_configuration.approve(artifact.pk, "other", 0, artifact.user.pk)
    newer = release_configuration.approve(artifact.pk, "other", saved.pk, artifact.user.pk)
    assert newer.pk != saved.pk and newer.target.slot != saved.target.slot
    saved.refresh_from_db()
    assert saved.specification["target_key"] == "mini-app-staging"


@pytest.mark.parametrize("change", ["port", "target", "digest", "specification"])
def test_configuration_changes_require_fresh_review(configured, monkeypatch, change):
    artifact = configured
    saved = release_configuration.approve(artifact.pk, "staging", 0, artifact.user.pk)
    if change == "port":
        monkeypatch.setenv("TEMPO_RELEASE_PORT", "8033")
    elif change == "target":
        DeploymentTarget.objects.filter(pk=saved.target_id).update(slot=uuid.uuid4())
    elif change == "digest":
        ReleaseConfiguration.objects.filter(pk=saved.pk).update(digest="a" * 64)
    else:
        ReleaseConfiguration.objects.filter(pk=saved.pk).update(specification={})
    assert not release_configuration.summary(artifact)["passed"]


@pytest.mark.parametrize(
    "key", ["", "../outside", "A", "a" * 49, "http://example.com", "bad;command"]
)
def test_target_names_are_data_and_cannot_select_host_paths_or_urls(configured, key):
    with pytest.raises(ValueError):
        release_configuration.approve(configured.pk, key, 0, configured.user.pk)
    assert DeploymentTarget.objects.count() == 0


def test_configuration_ui_requires_authentication_review_and_matching_artifact_scope(configured):
    artifact = configured
    url = artifact.url.replace("preview/", "configuration/")
    client = Client()
    assert client.get(url).status_code == 302
    client.force_login(artifact.user)
    assert client.get(url).status_code == 200
    data = {"target_key": "mini-app-staging", "expected_configuration_id": 0}
    assert client.post(url, data).status_code == 409
    assert not ReleaseConfiguration.objects.exists()
    data["reviewed"] = "on"
    assert client.post(url, data).status_code == 302
    response = client.get(url)
    assert "no-store" in response["Cache-Control"]
    assert b"Reserved address" in response.content
    assert (
        client.post(url.replace(f"runs/{artifact.run_id}/", "runs/999999/"), data).status_code
        == 404
    )
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(artifact.user)
    assert csrf.post(url, data).status_code == 403


def test_competing_approvals_cannot_overwrite_an_unseen_configuration(configured):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    barrier = threading.Barrier(2)

    def submit(key):
        try:
            barrier.wait(timeout=5)
            try:
                release_configuration.approve(configured.pk, key, 0, configured.user.pk)
                return "approved"
            except ValueError as exc:
                assert "changed" in str(exc)
                return "stale"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["staging-one", "staging-two"]))
    assert sorted(results) == ["approved", "stale"]
    assert ReleaseConfiguration.objects.count() == DeploymentTarget.objects.count() == 1
