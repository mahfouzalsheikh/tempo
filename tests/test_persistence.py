from pathlib import Path

import pytest

from tempo.domain import Issue, RunningEntry, utcnow
from tempo.persistence import PersistenceStore
from tempo_web.models import (
    AgentRun,
    AgentSession,
    RunNode,
    TrackedIssue,
    ValidationAttempt,
    ValidationCommand,
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_persists_run_session_and_validation_history(tmp_path: Path):
    issue = Issue(
        id="42",
        identifier="GH-42",
        title="Persist history",
        state="open",
        labels=["tempo"],
    )
    entry = RunningEntry(issue=issue, task=None, attempt=1)
    store = PersistenceStore("github")
    entry.run_record_id = await store.start_run(entry, tmp_path)

    entry.session.session_id = "session-1"
    entry.session.thread_id = "thread-1"
    entry.session.codex_total_tokens = 12
    entry.session.validation_status = "running"
    entry.session.validation_summary = "Project-native checks"
    entry.session.validation_started_at = utcnow()
    await store.record_event(
        entry,
        {"event": "validation_started", "summary": "Project-native checks"},
    )

    await store.record_event(
        entry,
        {
            "event": "validation_command_completed",
            "name": "Tests",
            "command": "pytest",
            "exit_code": 0,
            "output": "passed",
            "cleanup": False,
        },
    )
    entry.session.validation_status = "passed"
    entry.session.validation_finished_at = utcnow()
    await store.record_event(entry, {"event": "validation_completed", "success": True})
    await store.record_event(
        entry,
        {"event": "validation_fingerprint_recorded", "fingerprint": "a" * 64},
    )
    entry.phase = "NoChangesRequired"
    await store.finish_run(entry, status="succeeded", error=None)

    assert await TrackedIssue.objects.filter(identifier="GH-42").acount() == 1
    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert run.status == AgentRun.Status.SUCCEEDED
    assert run.total_tokens == 12
    assert await AgentSession.objects.filter(run=run, session_id="session-1").acount() == 1
    validation = await ValidationAttempt.objects.aget(run=run)
    assert validation.status == ValidationAttempt.Status.PASSED
    assert validation.workspace_fingerprint == "a" * 64
    command = await ValidationCommand.objects.aget(validation=validation)
    assert command.command == "pytest"
    assert command.exit_code == 0

    totals, completed = await store.runtime_summary()
    assert totals.total_tokens == 12
    assert totals.validation_passes == 1
    assert totals.validated_runs == 1
    assert totals.validation_failures == 0
    assert totals.runtime_seconds >= 0
    assert completed == 1
    assert await store.completed_issue_ids() == {"42"}

    interrupted_issue = Issue(
        id="43",
        identifier="GH-43",
        title="Interrupted",
        state="open",
    )
    interrupted = RunningEntry(
        issue=interrupted_issue,
        task=None,
        attempt=None,
    )
    interrupted.run_record_id = await store.start_run(interrupted, tmp_path / "interrupted")
    interrupted.session.validation_started_at = utcnow()
    await store.record_event(
        interrupted,
        {"event": "validation_started", "summary": "Interrupted validation"},
    )

    await store.reconcile_incomplete_records()

    interrupted_run = await AgentRun.objects.aget(pk=interrupted.run_record_id)
    interrupted_validation = await ValidationAttempt.objects.aget(run=interrupted_run)
    assert interrupted_run.status == AgentRun.Status.RETRY_SCHEDULED
    assert interrupted_run.phase == "Recovering"
    assert interrupted_validation.status == ValidationAttempt.Status.INVALIDATED


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_restarting_existing_run_clears_previous_error(tmp_path: Path):
    issue = Issue(id="restart", identifier="GH-RESTART", title="Continue", state="open")
    entry = RunningEntry(issue=issue, task=None, attempt=1)
    store = PersistenceStore("github")
    entry.run_record_id = await store.start_run(entry, tmp_path)
    await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
        status=AgentRun.Status.FAILED,
        phase="SafetyLimitReached",
        error="previous token-limit failure",
    )

    await store.start_run(entry, tmp_path)

    restarted = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert restarted.status == AgentRun.Status.RUNNING
    assert restarted.error == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_reset_incomplete_run_nodes_preserves_only_successes(tmp_path: Path):
    issue = Issue(id="graph", identifier="GH-GRAPH", title="Retry graph", state="open")
    entry = RunningEntry(issue=issue, task=None, attempt=1)
    store = PersistenceStore("github")
    entry.run_record_id = await store.start_run(entry, tmp_path)
    succeeded = await RunNode.objects.acreate(
        run_id=entry.run_record_id,
        node_key="plan",
        name="Plan",
        node_type="agent",
        status=RunNode.Status.SUCCEEDED,
        attempt=1,
        output={"summary": "done"},
    )
    skipped = await RunNode.objects.acreate(
        run_id=entry.run_record_id,
        node_key="publish",
        name="Publish",
        node_type="agent",
        status=RunNode.Status.SKIPPED,
        attempt=2,
        error="old failure",
        output={"reason": "dependency conditions did not match"},
    )

    await store.reset_incomplete_run_nodes(entry.run_record_id)

    await succeeded.arefresh_from_db()
    await skipped.arefresh_from_db()
    assert succeeded.status == RunNode.Status.SUCCEEDED
    assert succeeded.output == {"summary": "done"}
    assert skipped.status == RunNode.Status.PENDING
    assert skipped.attempt == 0
    assert skipped.error == ""
    assert skipped.output == {}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_clear_incomplete_run_node_context_preserves_completed_context(tmp_path: Path):
    issue = Issue(id="context", identifier="GH-CONTEXT", title="Fresh context", state="open")
    entry = RunningEntry(issue=issue, task=None, attempt=1)
    store = PersistenceStore("github")
    entry.run_record_id = await store.start_run(entry, tmp_path)
    succeeded = await RunNode.objects.acreate(
        run_id=entry.run_record_id,
        node_key="plan",
        name="Plan",
        node_type="agent",
        status=RunNode.Status.SUCCEEDED,
        session_id="plan-session",
        thread_id="plan-thread",
        total_tokens=10,
    )
    failed = await RunNode.objects.acreate(
        run_id=entry.run_record_id,
        node_key="implement",
        name="Implement",
        node_type="agent",
        status=RunNode.Status.FAILED,
        session_id="failed-session",
        thread_id="failed-thread",
        turn_count=1,
        total_tokens=900_000,
        thread_total_tokens=1_200_000,
    )

    await store.clear_incomplete_run_node_context(entry.run_record_id)

    await succeeded.arefresh_from_db()
    await failed.arefresh_from_db()
    assert succeeded.thread_id == "plan-thread"
    assert succeeded.total_tokens == 10
    assert failed.session_id == ""
    assert failed.thread_id == ""
    assert failed.turn_count == 0
    assert failed.total_tokens == 0
    assert failed.thread_total_tokens == 0
