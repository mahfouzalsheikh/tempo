from pathlib import Path

import pytest

from tempo.domain import Issue, RunningEntry, utcnow
from tempo.persistence import PersistenceStore
from tempo_web.models import (
    AgentRun,
    AgentSession,
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
    assert interrupted_run.status == AgentRun.Status.CANCELLED
    assert interrupted_run.phase == "Interrupted"
    assert interrupted_validation.status == ValidationAttempt.Status.INVALIDATED
