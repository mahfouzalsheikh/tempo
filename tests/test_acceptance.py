import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.test import Client
from django.utils import timezone
from test_previews import artifact as saved_artifact  # noqa: F401
from test_previews import bundle, product_factory  # noqa: F401

from tempo import acceptance
from tempo.acceptance_contract import RUNNER, digest, parse_steps, specification, verify_report
from tempo.acceptance_runner import CleanupError
from tempo_web.acceptance_views import summary
from tempo_web.models import AcceptanceAttempt, AcceptanceSuite, BuildArtifact

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def prepared(saved_artifact, tmp_path, monkeypatch):  # noqa: F811
    artifact = saved_artifact
    monkeypatch.setenv("TEMPO_ACCEPTANCE_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setenv("TEMPO_WORKSPACE_ROOT", str(tmp_path))
    instructions = {
        criterion["id"]: 'open /\nexpect text "Saved build"'
        for criterion in artifact.run.product_snapshot["brief"]["criteria"]
    }
    suite = acceptance.approve(artifact.pk, instructions, 0, artifact.user.pk)
    return artifact, suite, instructions


def queued(prepared, key=None):
    artifact, suite, _ = prepared
    return acceptance.enqueue(
        artifact.pk, suite.pk, suite.digest, key or uuid.uuid4(), artifact.user.pk
    )


def report_for(suite, artifact):
    return {
        "schema": 1,
        "runner": RUNNER,
        "artifact_digest": artifact.digest,
        "suite_digest": suite.digest,
        "browser": "test-browser",
        "results": [
            {
                "id": check["id"],
                "status": "passed",
                "error": "",
                "steps": [{"action": step, "status": "passed"} for step in check["steps"]],
            }
            for check in suite.specification["checks"]
        ],
    }


@pytest.mark.parametrize(
    "source",
    [
        "",
        'click button "Go"',
        'open https://example.com\nexpect text "Ready"',
        'open //evil.example\nexpect text "Ready"',
        'evaluate "document.body"',
        'open "/\\evil.example"\nexpect text "Ready"',
        'expect text ""',
        'expect text "Unclosed',
        "\n".join(['expect text "Ready"'] * 21),
    ],
)
def test_unsupported_or_vacuous_browser_journeys_are_rejected(source):
    with pytest.raises(ValueError):
        parse_steps(source)


def test_steps_are_data_and_cannot_execute_shell_syntax():
    assert parse_steps('fill "Name" "$(touch /tmp/owned)"\nexpect text "`id`"') == [
        ["fill", "Name", "$(touch /tmp/owned)"],
        ["expect", "text", "`id`"],
    ]


def test_every_criterion_requires_checks_and_approval_is_versioned(prepared):
    artifact, suite, instructions = prepared
    with pytest.raises(ValueError, match="every criterion"):
        specification(suite.specification["brief_digest"], [{"id": "missing"}], instructions)
    assert acceptance.approve(artifact.pk, instructions, suite.pk, artifact.user.pk).pk == suite.pk
    with pytest.raises(ValueError, match="changed"):
        acceptance.approve(artifact.pk, instructions, 0, artifact.user.pk)
    changed = dict.fromkeys(instructions, 'expect text "Changed"')
    next_suite = acceptance.approve(artifact.pk, changed, suite.pk, artifact.user.pk)
    assert next_suite.pk != suite.pk and next_suite.digest != suite.digest
    suite.refresh_from_db()
    assert suite.specification["checks"][0]["instructions"] != 'expect text "Changed"'
    assert AcceptanceSuite.objects.count() == 2
    with pytest.raises(ValueError, match="changed"):
        queued(prepared)


def test_queue_is_idempotent_and_uses_exact_identity(prepared):
    artifact, suite, _ = prepared
    key = uuid.uuid4()
    first = queued(prepared, key)
    assert queued(prepared, key).pk == first.pk
    assert queued(prepared).pk == first.pk
    assert first.identity == acceptance.identity(artifact, suite, first.image)
    running = acceptance.claim()
    assert running.pk == first.pk and running.lease_token and acceptance.claim() is None
    assert queued(prepared).pk == first.pk


def test_operator_checks_are_authenticated_scoped_and_csrf_protected(prepared):
    artifact, suite, _ = prepared
    url = artifact.url.replace("preview/", "acceptance/")
    client = Client(enforce_csrf_checks=True)
    assert client.get(url).status_code == 302
    client.force_login(artifact.user)
    response = client.get(url)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert b"Approve check plan" in response.content and b"Run reviewed checks" in response.content
    assert client.post(url, {"action": "run"}).status_code == 403
    assert client.get("/ideas/99999/" + url.split("/", 3)[3]).status_code == 404
    client = Client()
    client.force_login(artifact.user)
    assert client.post(url, {"action": "approve", "expected_suite_id": suite.pk}).status_code == 409
    assert (
        client.post(
            url,
            {
                "action": "run",
                "suite_id": suite.pk,
                "suite_digest": suite.digest,
                "request_key": uuid.uuid4(),
            },
        ).status_code
        == 302
    )


@pytest.mark.parametrize("field", ["artifact_digest", "suite_digest", "runner", "schema"])
def test_reports_must_match_exact_execution_identity(prepared, field):
    artifact, suite, _ = prepared
    report = report_for(suite, artifact)
    report[field] = "changed"
    with pytest.raises(ValueError):
        verify_report(report, suite.specification, artifact.digest)


@pytest.mark.parametrize("change", ["criterion", "missing", "steps", "action", "status"])
def test_partial_or_forged_passes_cannot_satisfy_acceptance(prepared, change):
    artifact, suite, _ = prepared
    report = report_for(suite, artifact)
    if change == "criterion":
        report["results"][0]["id"] = "not-in-brief"
    elif change == "missing":
        report["results"].pop()
    elif change == "steps":
        report["results"][0]["steps"].pop()
    elif change == "action":
        report["results"][0]["steps"][0]["action"] = ["expect", "text", "fake"]
    else:
        report["results"][0]["steps"][0]["status"] = "failed"
    with pytest.raises(ValueError):
        verify_report(report, suite.specification, artifact.digest)


def test_success_requires_current_evidence_and_new_plan_invalidates_coverage(prepared):
    artifact, suite, instructions = prepared
    queued(prepared)
    attempt = acceptance.claim()
    report = report_for(suite, artifact)
    assert acceptance.finish(attempt, report=report)
    assert summary(artifact)["passed"]
    assert not acceptance.finish(attempt, report=report)
    next_suite = acceptance.approve(
        artifact.pk, dict.fromkeys(instructions, 'expect text "New"'), suite.pk, artifact.user.pk
    )
    assert "passed" not in summary(artifact) and summary(artifact)["suite"].pk == next_suite.pk
    attempt.refresh_from_db()
    assert attempt.status == "passed" and attempt.report == report


@pytest.mark.parametrize("field", ["data", "report", "image"])
def test_tampered_evidence_is_not_displayed_as_passed(prepared, field):
    artifact, suite, _ = prepared
    queued(prepared)
    attempt = acceptance.claim()
    acceptance.finish(attempt, report=report_for(suite, artifact))
    if field == "data":
        BuildArtifact.objects.filter(pk=artifact.pk).update(data=b"corrupt")
        artifact.refresh_from_db()
    else:
        AcceptanceAttempt.objects.filter(pk=attempt.pk).update(
            **{field: {} if field == "report" else "sha256:" + "c" * 64},
        )
    assert summary(artifact)["status"] == "Evidence unavailable"


def test_expired_attempts_reject_late_results_and_recover_only_after_cleanup(prepared, monkeypatch):
    artifact, suite, _ = prepared
    queued(prepared)
    attempt = acceptance.claim()
    AcceptanceAttempt.objects.filter(pk=attempt.pk).update(
        deadline=timezone.now() - timedelta(seconds=1)
    )
    assert not acceptance.finish(attempt, report=report_for(suite, artifact))

    async def unavailable(_name):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(acceptance, "remove_container", unavailable)
    acceptance.recover()
    attempt.refresh_from_db()
    assert attempt.status == "running"

    async def cleaned(_name):
        pass

    monkeypatch.setattr(acceptance, "remove_container", cleaned)
    acceptance.recover()
    attempt.refresh_from_db()
    assert attempt.status == "interrupted"
    assert queued(prepared).pk != attempt.pk


@pytest.mark.parametrize("failure", [None, "check", "container", "cleanup", "identity"])
def test_worker_records_only_completed_cleaned_browser_evidence(
    prepared, monkeypatch, tmp_path, failure
):
    artifact, suite, _ = prepared
    queued(prepared)
    attempt = acceptance.claim()
    report = report_for(suite, artifact)
    if failure == "check":
        report["results"][0].update(status="failed", error="Expected text not found")
        report["results"][0]["steps"][-1]["status"] = "failed"
    if failure == "identity":
        attempt.identity = {"wrong": "identity"}

    async def execute(image, directory, key):
        assert image == attempt.image and key == attempt.request_key
        assert (directory / "artifact.zip").read_bytes() == bytes(artifact.data)
        if failure == "container":
            raise ValueError("runner failed")
        if failure == "cleanup":
            raise CleanupError("cleanup pending")
        return copy.deepcopy(report)

    monkeypatch.setattr(acceptance, "execute", execute)
    acceptance.process(attempt)
    attempt.refresh_from_db()
    assert (
        attempt.status
        == {
            None: "passed",
            "check": "failed",
            "container": "interrupted",
            "cleanup": "running",
            "identity": "interrupted",
        }[failure]
    )
    assert not (tmp_path / ".acceptance" / attempt.request_key.hex).exists()
    if failure in {"container", "cleanup", "identity"}:
        assert not attempt.report
    else:
        assert attempt.report_digest == digest(report)


def test_concurrent_operator_requests_queue_one_attempt(prepared):
    if connection.vendor != "postgresql":
        pytest.skip("Queue serialization requires PostgreSQL")
    barrier = threading.Barrier(2)

    def start(_):
        try:
            barrier.wait(timeout=10)
            return queued(prepared).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(start, range(2))
    assert first == second and AcceptanceAttempt.objects.count() == 1


def test_changed_checks_do_not_silently_reuse_an_older_active_run(prepared):
    artifact, suite, instructions = prepared
    queued(prepared)
    newer = acceptance.approve(
        artifact.pk, dict.fromkeys(instructions, 'expect text "New"'), suite.pk, artifact.user.pk
    )
    with pytest.raises(ValueError, match="earlier check plan"):
        acceptance.enqueue(artifact.pk, newer.pk, newer.digest, uuid.uuid4(), artifact.user.pk)


def test_corrupt_check_plan_fails_closed_and_can_be_replaced(prepared):
    artifact, suite, instructions = prepared
    AcceptanceSuite.objects.filter(pk=suite.pk).update(specification={})
    assert summary(artifact)["invalid"]
    client = Client()
    client.force_login(artifact.user)
    response = client.get(artifact.url.replace("preview/", "acceptance/"))
    assert response.status_code == 200 and b"Check plan unavailable" in response.content
    replacement = acceptance.approve(artifact.pk, instructions, suite.pk, artifact.user.pk)
    assert replacement.pk != suite.pk
