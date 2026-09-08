import copy

import pytest
from django.test import Client
from test_acceptance import prepared, queued, report_for  # noqa: F401
from test_acceptance_files import JOURNEY, file_report
from test_previews import artifact as saved_artifact  # noqa: F401
from test_previews import bundle, product_factory  # noqa: F401

from tempo import acceptance
from tempo.acceptance_contract import digest
from tempo_web.models import AcceptanceAttempt, BriefRevision, BuildArtifact, ExecutionPlan
from tempo_web.release_views import evaluate

pytestmark = pytest.mark.django_db(transaction=True)


def gates(report):
    return {gate["id"]: gate["status"] for gate in report["gates"]}


def test_v2_file_evidence_persists_and_reaches_the_current_assessment(prepared):  # noqa: F811
    artifact, old_suite, instructions = prepared
    suite = acceptance.approve(
        artifact.pk, dict.fromkeys(instructions, JOURNEY), old_suite.pk, artifact.user.pk
    )
    queued((artifact, suite, instructions))
    attempt = acceptance.claim()
    _, report = file_report()
    report.update(suite_digest=suite.digest, artifact_digest=artifact.digest)
    example = report["results"][0]
    report["results"] = [copy.deepcopy(example) | {"id": key} for key in instructions]
    assert acceptance.finish(attempt, report=report)
    attempt.refresh_from_db()
    assert attempt.status == "passed" and attempt.report == report
    assessment = evaluate(artifact)
    assert gates(assessment)["acceptance"] == "passed"
    assert assessment["acceptance"]["results"][0]["steps"][-1]["file"]["validator"] == "svg-v1"
    assert not assessment["ready"]


def test_passed_browser_checks_do_not_bypass_missing_deployment_gates(prepared):  # noqa: F811
    artifact, suite, instructions = prepared
    original = copy.deepcopy(artifact.manifest)
    queued(prepared)
    attempt = acceptance.claim()
    acceptance.finish(attempt, report=report_for(suite, artifact))
    result = evaluate(artifact)
    assert not result["ready"]
    assert gates(result) == {
        "candidate": "passed",
        "scope": "passed",
        "acceptance": "passed",
        "preview_health": "blocked",
        "target": "blocked",
        "rollback": "blocked",
    }
    assert result["acceptance"]["attempt_id"] == attempt.pk
    assert result["acceptance"]["report_digest"]
    acceptance.approve(
        artifact.pk, dict.fromkeys(instructions, 'expect text "New"'), suite.pk, artifact.user.pk
    )
    result = evaluate(artifact)
    assert gates(result)["acceptance"] == "blocked"
    assert result["acceptance"]["report_digest"] is None
    assert result["acceptance"]["results"] == []
    artifact.refresh_from_db()
    assert artifact.manifest == original


@pytest.mark.parametrize("change", ["brief", "plan", "approval", "corrupt_scope"])
def test_new_or_unapproved_scope_blocks_old_builds(prepared, change):  # noqa: F811
    artifact, _, _ = prepared
    plan = artifact.run.execution_plan
    if change == "brief":
        old = plan.brief_revision
        BriefRevision.objects.create(
            brief=old.brief,
            number=old.number + 1,
            specification=old.specification,
            digest=old.digest,
            created_by=artifact.user,
        )
    elif change == "plan":
        ExecutionPlan.objects.create(
            brief_revision=plan.brief_revision,
            number=plan.number + 1,
            specification=plan.specification,
            digest=plan.digest,
            generator="test",
            created_by=artifact.user,
        )
    elif change == "approval":
        ExecutionPlan.objects.filter(pk=plan.pk).update(approved_at=None)
    else:
        ExecutionPlan.objects.filter(pk=plan.pk).update(specification={})
    artifact = BuildArtifact.objects.get(pk=artifact.pk)
    result = evaluate(artifact)
    assert gates(result)["candidate"] == "passed"
    assert gates(result)["scope"] == "blocked" and not result["ready"]


@pytest.mark.parametrize("change", ["artifact", "report", "identity"])
def test_corrupt_evidence_cannot_produce_a_passed_release_gate(prepared, change):  # noqa: F811
    artifact, suite, _ = prepared
    queued(prepared)
    attempt = acceptance.claim()
    acceptance.finish(attempt, report=report_for(suite, artifact))
    if change == "artifact":
        BuildArtifact.objects.filter(pk=artifact.pk).update(data=b"corrupt")
    else:
        AcceptanceAttempt.objects.filter(pk=attempt.pk).update(**{change: {}})
    result = evaluate(BuildArtifact.objects.get(pk=artifact.pk))
    assert gates(result)["acceptance"] == "blocked"
    assert result["acceptance"]["report_digest"] is None
    assert not result["ready"]


def test_readiness_pages_and_exports_are_authenticated_scoped_and_read_only(prepared):  # noqa: F811
    artifact, _, _ = prepared
    url = artifact.url.replace("preview/", "readiness/")
    client = Client()
    assert client.get(url).status_code == 302
    client.force_login(artifact.user)
    response = client.get(url)
    assert response.status_code == 200 and b"Deployment blocked" in response.content
    assert "no-store" in response["Cache-Control"]
    response = client.get(url + "?format=json")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    exported = response.json()
    assert exported["digest"] == digest(exported["assessment"])
    assert exported["assessment"]["artifact_digest"] == artifact.digest
    assert not exported["assessment"]["ready"]
    assert client.post(url).status_code == 405
    assert client.get(url.replace(f"runs/{artifact.run_id}/", "runs/999999/")).status_code == 404
