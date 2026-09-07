import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from django.db import connection

from tempo.domain import Issue, RunningEntry, utcnow
from tempo.errors import LeaseLostError
from tempo.persistence import PersistenceStore
from tempo.trackers.github import GitHubTracker
from tempo_web.models import AgentRun, RunCheckpoint, RunNode, ValidationAttempt, WorkerLease

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


@pytest.fixture
async def claimed_run(tmp_path):
    store = PersistenceStore("memory")
    await store.initialize()
    issue = Issue(id="lease", identifier="LEASE-1", title="Lease test", state="open")
    run_id = await store.enqueue_issue(issue)
    token = await store.claim_run(run_id, "original-worker")
    entry = RunningEntry(issue=issue, task=None, attempt=1, run_record_id=run_id, lease_token=token)
    await store.start_run(entry, tmp_path)
    await RunNode.objects.acreate(run_id=run_id, node_key="work", name="Work", node_type="agent")
    return store, entry


async def reclaim(store, entry):
    await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
        lease_expires_at=utcnow() - timedelta(seconds=1),
    )
    replacement = await store.claim_run(entry.run_record_id, "replacement-worker")
    assert replacement and replacement != entry.lease_token
    return replacement


@pytest.mark.parametrize("operation", [
    "heartbeat", "start", "nodes", "node_start", "node_finish", "node_reset", "node_model",
    "node_wait", "node_event", "event", "checkpoint", "approval", "resume", "feedback",
    "finish", "retry", "validation", "issue", "resume_context",
])
async def test_stale_worker_cannot_mutate_reclaimed_run(claimed_run, tmp_path, operation):
    store, entry = claimed_run
    run_id, token = entry.run_record_id, entry.lease_token
    replacement = await reclaim(store, entry)
    before = await AgentRun.objects.values().aget(pk=run_id)
    node_before = await RunNode.objects.values().aget(run_id=run_id)
    operations = {
        "heartbeat": lambda: store.heartbeat(run_id, lease_token=token),
        "start": lambda: store.start_run(entry, tmp_path),
        "nodes": lambda: store.initialize_run_nodes(entry),
        "node_start": lambda: store.start_run_node(run_id, "work", 2, lease_token=token),
        "node_finish": lambda: store.finish_run_node(
            run_id, "work", status="succeeded", lease_token=token,
        ),
        "node_reset": lambda: store.reset_incomplete_run_nodes(run_id, lease_token=token),
        "node_model": lambda: store.set_run_node_model(run_id, "work", "other", lease_token=token),
        "node_wait": lambda: store.set_run_node_waiting(run_id, "work", lease_token=token),
        "node_event": lambda: store.record_run_node_event(
            run_id, "work", entry.session, lease_token=token,
        ),
        "event": lambda: store.record_event(entry, {"event": "validation_started"}),
        "checkpoint": lambda: store.checkpoint(
            run_id, "late", {}, idempotency_key="late", lease_token=token,
        ),
        "approval": lambda: store.create_approval(
            run_id, request_key="late", kind="tool", details={}, lease_token=token,
        ),
        "resume": lambda: store.resume_after_approval(run_id, lease_token=token),
        "resume_context": lambda: store.resume_context(run_id, lease_token=token),
        "feedback": lambda: store.clear_run_feedback(run_id, lease_token=token),
        "finish": lambda: store.finish_run(entry, status="succeeded", error=None),
        "retry": lambda: store.finish_run(
            entry, status="retry_scheduled", error="late", retry_attempt=2, retry_due_at=utcnow(),
        ),
        "validation": lambda: store.invalidate_validation_recovery(
            run_id, fingerprint="x", node_ids={"work"}, lease_token=token,
        ),
        "issue": lambda: store.sync_run_issue(entry, entry.issue),
    }
    with pytest.raises(LeaseLostError):
        await operations[operation]()
    assert await AgentRun.objects.values().aget(pk=run_id) == before
    assert await RunNode.objects.values().aget(run_id=run_id) == node_before
    assert await RunCheckpoint.objects.filter(run_id=run_id).acount() == 0
    assert (await WorkerLease.objects.aget(token=replacement)).released_at is None


async def test_expired_or_missing_token_cannot_renew_lease(claimed_run):
    store, entry = claimed_run
    with pytest.raises(LeaseLostError):
        await store.heartbeat(entry.run_record_id)
    await store.heartbeat(entry.run_record_id, lease_token=entry.lease_token)
    expired = utcnow() - timedelta(seconds=1)
    await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(lease_expires_at=expired)
    with pytest.raises(LeaseLostError):
        await store.heartbeat(entry.run_record_id, lease_token=entry.lease_token)
    assert (await AgentRun.objects.aget(pk=entry.run_record_id)).lease_expires_at == expired


async def test_completion_schedules_retry_and_releases_owner_atomically(claimed_run):
    store, entry = claimed_run
    due_at = utcnow() + timedelta(seconds=10)
    await store.finish_run(
        entry, status="retry_scheduled", error="retry", retry_attempt=2, retry_due_at=due_at,
    )
    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert (run.status, run.attempt, run.available_at) == ("retry_scheduled", 2, due_at)
    assert not run.lease_token and run.lease_expires_at is None and run.finished_at is None
    assert (await WorkerLease.objects.aget(token=entry.lease_token)).released_at is not None
    with pytest.raises(LeaseLostError):
        await store.finish_run(entry, status="failed", error="late duplicate")
    with pytest.raises(LeaseLostError):
        await store.heartbeat(entry.run_record_id)


async def test_scheduler_cannot_overwrite_a_worker_claim(claimed_run):
    store, entry = claimed_run
    with pytest.raises(LeaseLostError):
        await store.schedule_retry(entry.run_record_id, attempt=2, due_at=utcnow(), error="stale")
    assert (await AgentRun.objects.aget(pk=entry.run_record_id)).lease_token == entry.lease_token


async def test_checkpoint_replay_does_not_rewind_pointer(claimed_run):
    store, entry = claimed_run
    for key in ("first", "second", "first"):
        await store.checkpoint(
            entry.run_record_id, key, {}, idempotency_key=key, lease_token=entry.lease_token,
        )
    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert run.checkpoint["sequence"] == 2 and run.checkpoint["kind"] == "second"
    assert await RunCheckpoint.objects.filter(run=run).acount() == 2


async def test_provider_mutations_recheck_owner_and_use_separate_publication_gates(claimed_run):
    store, entry = claimed_run
    requests = []

    async def ownership():
        await store.assert_ownership(entry.run_record_id, lease_token=entry.lease_token)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tracker = GitHubTracker(repo="acme/project", client=client)
        old = tracker.for_run(ownership)
        old.authorize_publication(entry.issue.id)
        replacement = await reclaim(store, entry)

        async def new_ownership():
            await store.assert_ownership(entry.run_record_id, lease_token=replacement)

        new = tracker.for_run(new_ownership)
        issue = Issue(id="1", identifier="GH-1", title="Comment", state="open")
        arguments = {"body": "Progress"}
        assert entry.issue.id not in new._publication_authorized
        new.authorize_publication(entry.issue.id)
        old.revoke_publication(entry.issue.id)
        assert entry.issue.id in new._publication_authorized
        assert (await new.execute_agent_tool("github_comment", arguments, issue))["success"]
        old.authorize_publication(entry.issue.id)
        with pytest.raises(LeaseLostError):
            await old.execute_agent_tool("github_comment", arguments, issue)
        # The same guard applies to control-plane review, merge and finalization requests.
        with pytest.raises(LeaseLostError):
            await old._request("PUT", "/repos/acme/project/pulls/1/merge", json={})
    assert len(requests) == 2


async def test_heartbeat_loss_cancels_worker_without_overwriting_replacement(claimed_run, tmp_path):
    from tempo.orchestrator import Orchestrator

    store, entry = claimed_run
    orchestrator = Orchestrator(str(tmp_path / "WORKFLOW.md"))
    orchestrator.persistence = store
    entry.task = asyncio.create_task(asyncio.Event().wait())
    orchestrator.running[entry.issue.id] = entry
    orchestrator.claimed.add(entry.issue.id)
    replacement = await reclaim(store, entry)
    await orchestrator._heartbeat_worker(entry)
    await asyncio.gather(entry.task, return_exceptions=True)
    assert entry.task.cancelled() and entry.lease_lost
    await orchestrator._worker_finished(entry.issue.id, entry.task)
    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert run.lease_token == replacement and run.status == "running"
    assert entry.issue.id not in orchestrator.running
    assert entry.issue.id not in orchestrator.retries
    assert entry.issue.id not in orchestrator.safety_blocked


@pytest.mark.parametrize("callback", ["event", "approval"])
async def test_delayed_callback_cannot_adopt_replacement_entry(claimed_run, tmp_path, callback):
    from tempo.orchestrator import Orchestrator

    store, old_entry = claimed_run
    token = await reclaim(store, old_entry)
    replacement = RunningEntry(
        issue=old_entry.issue, task=None, attempt=2,
        run_record_id=old_entry.run_record_id, lease_token=token,
    )
    orchestrator = Orchestrator(str(tmp_path / "WORKFLOW.md"))
    orchestrator.persistence = store
    orchestrator.running[replacement.issue.id] = replacement
    with pytest.raises(LeaseLostError):
        if callback == "event":
            await orchestrator._codex_event(
                old_entry.issue.id, {"event": "validation_started"}, expected_entry=old_entry,
            )
        else:
            await orchestrator._wait_for_approval(
                old_entry.issue.id, "tool", {}, expected_entry=old_entry,
            )
    assert replacement.session.last_codex_event is None
    assert (await AgentRun.objects.aget(pk=old_entry.run_record_id)).status == "running"


@pytest.mark.parametrize("status", ["running", "waiting_approval"])
async def test_recovery_releases_expired_owner_and_invalidates_its_validation(claimed_run, status):
    store, entry = claimed_run
    validation = await ValidationAttempt.objects.acreate(
        run_id=entry.run_record_id, status="running", started_at=utcnow(),
    )
    await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(status=status)
    await store.reconcile_incomplete_records()
    await validation.arefresh_from_db()
    assert validation.status == "running"
    assert (await WorkerLease.objects.aget(token=entry.lease_token)).released_at is None

    await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
        lease_expires_at=utcnow() - timedelta(seconds=1),
    )
    await store.reconcile_incomplete_records()
    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert run.status == ("retry_scheduled" if status == "running" else "waiting_approval")
    assert not run.lease_token and run.lease_expires_at is None
    assert (await WorkerLease.objects.aget(token=entry.lease_token)).released_at is not None
    await validation.arefresh_from_db()
    assert validation.status == "invalidated"
    with pytest.raises(LeaseLostError):
        await store.heartbeat(entry.run_record_id, lease_token=entry.lease_token)


async def run_database_workers(mode, entry):
    db = connection.settings_dict
    env = {
        **os.environ,
        "DATABASE_URL": (
            f"postgresql://{quote(db['USER'], safe='')}:{quote(db['PASSWORD'], safe='')}"
            f"@{db['HOST']}:{db['PORT']}/{quote(db['NAME'], safe='')}"
        ),
        "TEMPO_TEST_LEASE_TOKEN": entry.lease_token,
    }
    script = Path(__file__).parent / "fixtures" / "lease_race_worker.py"
    workers = []
    try:
        for index in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(script), mode, str(entry.run_record_id), f"worker-{index}",
                env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            workers.append(process)
        for process in workers:
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        for process in workers:
            process.stdin.write(b"go\n")
            await process.stdin.drain()
        results = []
        for process in workers:
            output, error = await asyncio.wait_for(process.communicate(), 20)
            assert process.returncode == 0, error.decode()
            results.append(json.loads(output))
        return results
    finally:
        for process in workers:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks")
async def test_independent_processes_allocate_unique_checkpoint_sequences(claimed_run):
    store, entry = claimed_run
    assert await run_database_workers("checkpoint", entry) == [{"written": 20}, {"written": 20}]
    sequences = [
        row.sequence async for row in RunCheckpoint.objects.filter(run_id=entry.run_record_id)
    ]
    assert sequences == list(range(1, 41))
    assert (await AgentRun.objects.aget(pk=entry.run_record_id)).checkpoint["sequence"] == 40


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks")
async def test_only_one_process_can_reclaim_an_expired_run(claimed_run):
    store, entry = claimed_run
    await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
        lease_expires_at=utcnow() - timedelta(seconds=1),
    )
    results = await run_database_workers("claim", entry)
    assert sum(result["claimed"] for result in results) == 1
    active_leases = WorkerLease.objects.filter(run_id=entry.run_record_id, released_at=None)
    assert await active_leases.acount() == 1
    with pytest.raises(LeaseLostError):
        await store.heartbeat(entry.run_record_id, lease_token=entry.lease_token)
