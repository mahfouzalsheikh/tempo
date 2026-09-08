import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import HTTPServer

import pytest
from django.db import connection, connections
from django.test import Client
from django.utils import timezone
from test_previews import artifact as saved_artifact  # noqa: F401
from test_previews import bundle, product_factory  # noqa: F401

from tempo import preview_health
from tempo.acceptance_contract import digest
from tempo.preview_probe import expected_report
from tempo.preview_server import PreviewHandler
from tempo_web.models import PreviewDeployment, PreviewHealthAttempt
from tempo_web.release_views import evaluate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def published(saved_artifact, monkeypatch):  # noqa: F811
    client = Client()
    client.force_login(saved_artifact.user)
    assert client.post(saved_artifact.url + "start/").status_code == 302
    deployment = PreviewDeployment.objects.get(artifact=saved_artifact)
    server = HTTPServer(("127.0.0.1", 0), PreviewHandler)
    server.root, server.public_port = saved_artifact.root, 8031
    monkeypatch.setenv("TEMPO_PREVIEW_HEALTH_HOST", "127.0.0.1")
    monkeypatch.setenv("TEMPO_PREVIEW_HEALTH_PORT", str(server.server_port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield saved_artifact, deployment, client
    server.shutdown()
    server.server_close()
    thread.join()


def enqueue(published, key=None):
    artifact, deployment, _ = published
    return preview_health.enqueue(
        artifact.pk, preview_health.generation(deployment), key or uuid.uuid4(), artifact.user.pk
    )


def passed(published):
    enqueue(published)
    attempt = preview_health.claim()
    preview_health.process(attempt)
    attempt.refresh_from_db()
    assert attempt.status == "passed", attempt.error
    return attempt


def test_real_http_bytes_and_security_headers_satisfy_only_the_preview_gate(published):
    artifact, deployment, _ = published
    attempt = passed(published)
    info = preview_health.summary(artifact)
    assert info["passed"] and info["valid_until"] <= attempt.finished_at + timedelta(minutes=5)
    assert attempt.report["checks"] == expected_report(artifact.manifest["files"])
    assert attempt.report_digest == digest(attempt.report)
    report = evaluate(artifact)
    assert report["schema"] == 4 and report["evaluator"] == "release-readiness-v4"
    assert {g["id"]: g["status"] for g in report["gates"]}["preview_health"] == "passed"
    assert not report["ready"]
    assert deployment.token.hex not in str(report)


@pytest.mark.parametrize(
    "change", ["stop", "renew", "archive", "metadata", "expiry", "report", "time"]
)
def test_old_receipts_fail_closed_after_preview_or_evidence_changes(published, change):
    artifact, deployment, client = published
    attempt = passed(published)
    if change in {"stop", "renew"}:
        client.post(artifact.url + "stop/")
        if change == "renew":
            client.post(artifact.url + "start/")
    elif change == "archive":
        (artifact.root / deployment.token.hex / "artifact.zip").write_bytes(b"corrupt")
    elif change == "metadata":
        (artifact.root / deployment.token.hex / "metadata.json").write_text("{}")
    elif change == "expiry":
        PreviewDeployment.objects.filter(pk=deployment.pk).update(expires_at=timezone.now())
    elif change == "report":
        PreviewHealthAttempt.objects.filter(pk=attempt.pk).update(report={})
    else:
        PreviewHealthAttempt.objects.filter(pk=attempt.pk).update(finished_at=timezone.now())
    assert not preview_health.summary(artifact)["passed"]
    assert not evaluate(artifact)["preview_health"]["passed"]


def test_health_receipts_expire_and_new_failed_checks_do_not_inherit_a_pass(published, monkeypatch):
    artifact, _, _ = published
    attempt = passed(published)
    now = timezone.now
    monkeypatch.setattr(preview_health.timezone, "now", lambda: now() + timedelta(minutes=6))
    assert not preview_health.summary(artifact)["passed"]
    monkeypatch.setattr(preview_health.timezone, "now", now)
    enqueue(published)
    current = preview_health.claim()
    assert not preview_health.summary(artifact)["passed"]
    preview_health.finish(current, error="Deliberate failed check")
    assert preview_health.summary(artifact)["status"] == "failed"
    attempt.refresh_from_db()
    assert attempt.status == "passed"


def test_health_requests_are_idempotent_and_cannot_check_a_replacement_from_a_stale_page(published):
    artifact, deployment, client = published
    key = uuid.uuid4()
    first = enqueue(published, key)
    assert enqueue(published, key).pk == first.pk
    assert enqueue(published).pk == first.pk
    client.post(artifact.url + "stop/")
    client.post(artifact.url + "start/")
    with pytest.raises(ValueError, match="preview changed"):
        enqueue(published)
    attempt = preview_health.claim()
    preview_health.process(attempt)
    attempt.refresh_from_db()
    assert attempt.status == "interrupted"
    deployment.refresh_from_db()
    assert enqueue(published).pk != first.pk


def test_lease_expiring_during_final_artifact_verification_rejects_success(published, monkeypatch):
    artifact, _, _ = published
    enqueue(published)
    attempt = preview_health.claim()
    clock = [timezone.now()]
    monkeypatch.setattr(preview_health.timezone, "now", lambda: clock[0])

    def slow_verification(_artifact):
        clock[0] += timedelta(seconds=90)

    monkeypatch.setattr(preview_health, "verified_candidate_artifact", slow_verification)
    report = {
        "identity": attempt.identity,
        "checks": expected_report(artifact.manifest["files"]),
        "worker_revision": "test",
    }
    assert not preview_health.finish(attempt, report)


@pytest.mark.parametrize("change", ["report", "lease", "deadline", "renew"])
def test_late_or_incomplete_results_cannot_become_authoritative(published, change):
    artifact, _, client = published
    enqueue(published)
    attempt = preview_health.claim()
    report = {
        "identity": attempt.identity,
        "checks": expected_report(artifact.manifest["files"]),
        "worker_revision": "test",
    }
    if change == "report":
        report["checks"]["file_count"] = 0
        with pytest.raises(ValueError):
            preview_health.finish(attempt, report)
    elif change in {"lease", "deadline"}:
        field = "lease_token" if change == "lease" else "deadline"
        value = uuid.uuid4() if change == "lease" else timezone.now() - timedelta(seconds=1)
        PreviewHealthAttempt.objects.filter(pk=attempt.pk).update(**{field: value})
        assert not preview_health.finish(attempt, report)
        if change == "deadline":
            preview_health.recover()
            attempt.refresh_from_db()
            assert attempt.status == "interrupted"
    else:
        client.post(artifact.url + "stop/")
        client.post(artifact.url + "start/")
        preview_health.finish(attempt, report)
    assert not preview_health.summary(artifact)["passed"]


def test_preview_health_endpoint_is_authenticated_scoped_csrf_protected_and_post_only(published):
    artifact, deployment, client = published
    url = artifact.url.replace("preview/", "health/")
    data = {"request_key": uuid.uuid4(), "generation": preview_health.generation(deployment)}
    assert Client().post(url, data).status_code == 302
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(artifact.user)
    assert csrf.post(url, data).status_code == 403
    assert client.get(url).status_code == 405
    assert (
        client.post(url.replace(f"runs/{artifact.run_id}/", "runs/999999/"), data).status_code
        == 404
    )
    assert client.post(url, data).status_code == 302
    response = client.get(artifact.url.replace("preview/", "readiness/"))
    assert b"Check preview health" in response.content and "no-store" in response["Cache-Control"]


def test_two_concurrent_submissions_share_one_active_probe(published):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    artifact, deployment, _ = published
    barrier = threading.Barrier(2)

    def submit():
        try:
            barrier.wait(timeout=5)
            return preview_health.enqueue(
                artifact.pk, preview_health.generation(deployment), uuid.uuid4(), artifact.user.pk
            ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0] == results[1]
