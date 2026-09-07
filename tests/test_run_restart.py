import asyncio
import inspect
from types import SimpleNamespace

import pytest
import yaml
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client
from test_run_snapshot import configuration, definition

from tempo.agent_runtime import providers
from tempo.domain import Issue
from tempo.errors import LeaseLostError
from tempo.orchestrator import Orchestrator
from tempo.persistence import PersistenceStore
from tempo.run_snapshot import snapshot_digest
from tempo.runtime import set_orchestrator
from tempo_web.models import AgentRun, ApprovalRequest, OperatorAction, RunNode

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def setup_restart(tmp_path):
    config, source = configuration(tmp_path), definition(tmp_path)
    store = PersistenceStore("memory", config=config, definition=source)
    await store.initialize()
    issue = Issue(id="one", identifier="A-1", title="First", state="Todo")
    run_id = await store.enqueue_issue(issue)
    await AgentRun.objects.filter(pk=run_id).aupdate(
        status="failed", execution_snapshot={}, snapshot_digest="", error="snapshot_missing",
        checkpoint={"publication": {"approved": True}}, total_tokens=50,
    )
    user = await get_user_model().objects.acreate_user(username="restart-operator")
    kwargs = dict(user_id=user.pk, idempotency_key="restart-once",
                  expected_snapshot_digest=snapshot_digest(store.execution_snapshot))
    return store, run_id, kwargs


async def test_restart_preserves_history_and_creates_only_one_fresh_successor(tmp_path):
    store, run_id, kwargs = await setup_restart(tmp_path)
    old = await AgentRun.objects.aget(pk=run_id)
    approval = await ApprovalRequest.objects.acreate(
        run=old, request_key="old-approval", kind="tool", title="Publish",
    )
    result = await store.restart_run(run_id, **kwargs)
    assert result[0]
    assert await store.restart_run(run_id, **kwargs) == result
    assert await store.restart_run(run_id, **{**kwargs, "idempotency_key": "another"}) == (
        False, "run_superseded",
    )
    successor = await AgentRun.objects.aget(restarted_from_id=run_id)
    await old.arefresh_from_db()
    await approval.arefresh_from_db()
    assert old.execution_snapshot == {} and old.snapshot_digest == ""
    assert old.error == "snapshot_missing" and old.checkpoint["publication"]["approved"]
    assert old.status == "cancelled" and old.total_tokens == 50
    assert old.idempotency_key is None
    assert approval.status == "cancelled"
    assert successor.snapshot_digest == kwargs["expected_snapshot_digest"]
    assert successor.checkpoint == {} and successor.total_tokens == 0
    assert successor.workspace_path == "" and successor.fresh_workspace_key.startswith("restart-")
    assert successor.pull_request_url == "" and successor.feedback == ""
    assert not await successor.node_runs.aexists()
    assert not await successor.approvals.aexists()
    assert not await successor.validations.aexists()
    assert await OperatorAction.objects.acount() == 1
    assert await store.claim_run(run_id, "stale-worker") is None
    with pytest.raises(LeaseLostError):
        await store.set_control_state(run_id, status="retry_scheduled")
    with pytest.raises(LeaseLostError):
        await store.clear_incomplete_run_node_context(run_id)
    assert await store.enqueue_issue(await store.queued_issue(successor.pk)) == successor.pk
    assert (await store.for_execution(successor.pk))[0].fresh_workspace_key == (
        successor.fresh_workspace_key
    )


@pytest.mark.parametrize("case", ["running", "succeeded", "lease", "changed", "identity"])
async def test_restart_rejections_leave_original_untouched(tmp_path, case):
    store, run_id, kwargs = await setup_restart(tmp_path)
    if case in {"running", "succeeded"}:
        await AgentRun.objects.filter(pk=run_id).aupdate(status=case)
    elif case == "lease":
        await AgentRun.objects.filter(pk=run_id).aupdate(lease_token="still-owned")
    elif case == "changed":
        kwargs["expected_snapshot_digest"] = "old-digest"
    else:
        store.project_id += 1000
    result = await store.restart_run(run_id, **kwargs)
    assert not result[0]
    assert await AgentRun.objects.acount() == 1
    assert await OperatorAction.objects.acount() == 0
    assert (await AgentRun.objects.aget(pk=run_id)).idempotency_key is not None


async def test_restart_api_shows_legacy_work_and_checks_confirmation_identity(tmp_path):
    store, run_id, kwargs = await setup_restart(tmp_path)
    controller = SimpleNamespace(persistence=store)

    async def control(run_id, action, payload, **options):
        return await store.restart_run(run_id, **options, **payload)

    controller.control_run = control
    set_orchestrator(controller)
    client = Client()
    try:
        denied = await sync_to_async(client.post)(f"/api/v1/runs/{run_id}/restart")
        assert denied.status_code == 401
        user = await get_user_model().objects.aget(pk=kwargs["user_id"])
        await sync_to_async(client.force_login)(user)
        response = await sync_to_async(client.get)("/api/v1/control")
        row = response.json()["runs"][0]
        assert row["can_restart"] and row["snapshot_digest"] == ""
        assert row["restart_snapshot_digest"] == kwargs["expected_snapshot_digest"]
        url = f"/api/v1/runs/{run_id}/restart"
        options = {"content_type": "application/json", "HTTP_IDEMPOTENCY_KEY": "api-restart"}
        response = await sync_to_async(client.post)(url, data={
            "expected_snapshot_digest": row["restart_snapshot_digest"],
        }, **options)
        assert response.status_code == 202
        assert (await sync_to_async(client.post)(url, data={
            "expected_snapshot_digest": "different",
        }, **options)).status_code == 409
        assert (await sync_to_async(client.get)("/api/v1/control")).json()["runs"] == []
    finally:
        set_orchestrator(None)


async def test_restarted_worker_uses_new_checkout_prompt_and_node_context(tmp_path, monkeypatch):
    config, source = configuration(tmp_path), definition(tmp_path)
    config.hooks.after_create = "echo fresh > marker"
    source.path.write_text("---\n" + yaml.safe_dump(config.model_dump(mode="json", by_alias=True))
                           + "---\nCURRENT PROMPT")
    controller = Orchestrator(str(source.path))
    await controller.store.initialize()
    await controller._apply_config(controller.store.current()[1])
    issue = Issue(id="one", identifier="A-1", title="First", state="Todo")
    old_id = await controller.persistence.enqueue_issue(issue)
    old_path = config.workspace.root / issue.identifier
    old_path.mkdir(parents=True)
    (old_path / "marker").write_text("old work")
    await AgentRun.objects.filter(pk=old_id).aupdate(
        status="failed", workspace_path=str(old_path), execution_snapshot={}, snapshot_digest="",
    )
    user = await get_user_model().objects.acreate_user(username="operator")
    result = await controller.control_run(old_id, "restart", {
        "expected_snapshot_digest": snapshot_digest(controller.persistence.execution_snapshot),
    }, user_id=user.pk, idempotency_key="worker-restart")
    assert result[0]
    successor = await AgentRun.objects.aget(restarted_from_id=old_id)
    seen = []

    def create_runtime(_config, _profile, _manager, _tracker, on_event, *_args, **_kwargs):
        class Runtime:
            async def start_session(self, workspace, **_kwargs):
                assert workspace != old_path
                assert workspace.name == successor.fresh_workspace_key
                assert (workspace / "marker").read_text().strip() == "fresh"
                return SimpleNamespace(resumed=False)

            async def run_turn(self, session, prompt, _issue):
                seen.append(prompt)
                await on_event({"event": "turn/completed"})

            async def stop_session(self, _session):
                pass
        return Runtime()

    monkeypatch.setattr(providers, "create_runtime", create_runtime)
    token = await controller.persistence.claim_run(successor.pk, "test")
    controller._dispatch_locked(issue, 0, run_record_id=successor.pk, lease_token=token)
    task = controller.running[issue.id].task
    await asyncio.wait_for(task, 10)
    await controller._worker_finished(issue.id, task)
    assert len(seen) == 1 and "CURRENT PROMPT" in seen[0]
    assert (old_path / "marker").read_text() == "old work"
    assert (await RunNode.objects.aget(run_id=successor.pk)).status == "succeeded"
    assert await controller.control_run(old_id, "retry", {}, user_id=user.pk,
                                        idempotency_key="old-retry") == (False, "run_superseded")
    await controller.stop()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks")
async def test_concurrent_restart_requests_share_one_successor(tmp_path):
    store, run_id, kwargs = await setup_restart(tmp_path)
    # Independent DB connections in separate threads contend for the same source row.
    def restart():
        try:
            return inspect.getattr_static(PersistenceStore, "restart_run").func(
                store, run_id, **kwargs,
            )
        finally:
            connections.close_all()

    results = await asyncio.gather(*[
        sync_to_async(restart, thread_sensitive=False)()
        for _ in range(2)
    ])
    assert all(result[0] for result in results)
    assert results[0] == results[1]
    assert await AgentRun.objects.filter(restarted_from_id=run_id).acount() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks")
async def test_restart_and_claim_cannot_both_own_the_source(tmp_path):
    import threading

    store, run_id, kwargs = await setup_restart(tmp_path)
    await AgentRun.objects.filter(pk=run_id).aupdate(status="queued")
    barrier = threading.Barrier(2, timeout=10)

    def race(restart):
        try:
            barrier.wait()
            if restart:
                return inspect.getattr_static(PersistenceStore, "restart_run").func(
                store, run_id, **kwargs,
            )
            return inspect.getattr_static(PersistenceStore, "claim_run").func(
                store, run_id, "competing-worker",
            )
        finally:
            connections.close_all()

    restarted, claimed = await asyncio.gather(*[
        sync_to_async(race, thread_sensitive=False)(restart) for restart in (True, False)
    ])
    assert bool(restarted[0]) != bool(claimed)
    assert await AgentRun.objects.filter(restarted_from_id=run_id).acount() == int(restarted[0])
