import asyncio
import copy
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
import yaml
from asgiref.sync import sync_to_async
from django.db import connection, connections
from django.test import Client
from test_product_execution import enqueue as start_product
from test_product_execution import factory as product_factory

from tempo.agent_runtime import providers
from tempo.errors import CodexError, ConfigError, WorkspaceError
from tempo.intake import IntakeConflict
from tempo.integration import git, inspect_repository
from tempo.orchestrator import Orchestrator
from tempo.product_repairs import actions, check_candidate, enqueue, evidence, review
from tempo_web.models import AgentRun, OperatorAction, RunCheckpoint

factory = product_factory

pytestmark = pytest.mark.django_db(transaction=True)


async def dispatch(controller, run):
    await run.arefresh_from_db()
    issue = await controller.persistence.queued_issue(run.pk)
    token = await controller.persistence.claim_run(run.pk, "repair-test")
    assert token
    controller._dispatch_locked(issue, run.attempt, run_record_id=run.pk, lease_token=token)
    task = controller.running[issue.id].task
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 25)
    await controller._worker_finished(issue.id, task)
    await run.arefresh_from_db()


async def stopped_build(factory, monkeypatch, *, dirty=False, behavior="fix"):
    factory.config.validation.required_checks[0].command += " && test -f repaired.txt"
    if dirty:
        factory.config.hooks.after_run += (
            "\nif ! test -f repaired.txt; then echo generated >> base.txt; fi"
        )
    factory.source.path.write_text(
        "---\n" + yaml.safe_dump(factory.config.model_dump(mode="json", by_alias=True))
        + "---\nSOURCE PROMPT"
    )
    controller = Orchestrator(str(factory.source.path))
    await controller.store.initialize()
    await controller._apply_config(controller.store.current()[1])
    factory.store = controller.persistence
    run = await sync_to_async(start_product)(factory)
    turns = []

    def runtime(_config, profile, _manager, _tracker, on_event, *_args, **_kwargs):
        class Runtime:
            async def start_session(self, workspace, **_kwargs):
                return SimpleNamespace(workspace=workspace, resumed=False)

            async def run_turn(self, session, prompt, _issue):
                repair = '"title": "Build repair ' in prompt.split("Assigned product task")[-1]
                turns.append("repair" if repair else profile.role)
                path = session.workspace
                await on_event({"event": "turn/completed", "usage": {"total_tokens": 3}})
                if repair:
                    assert '"paths": ["repaired.txt"]' in prompt
                    assert "SAVED ROLE" in prompt
                    if behavior == "interrupt":
                        raise asyncio.CancelledError()
                    if behavior == "forge":
                        await on_event({"event": "validation_completed", "success": True})
                    (path / "repaired.txt").write_text("fixed")
                    if dirty:
                        (path / "base.txt").write_text("base\n")
                    if behavior == "outside":
                        (path / "outside.txt").write_text("unreviewed")
                    if behavior == "space":
                        (path / " repaired.txt").write_text("unreviewed")
                    await git(path, "add", ".")
                    await git(path, "commit", "-m", "Scoped build repair")
                elif profile.role == "planner":
                    for name in ("left.txt", "right.txt"):
                        (path / name).write_text(name)
                    await git(path, "add", ".")
                    await git(path, "commit", "-m", "Initial candidate")

            async def stop_session(self, _session):
                pass

        return Runtime()

    monkeypatch.setattr(providers, "create_runtime", runtime)
    await dispatch(controller, run)
    assert run.status == "failed", run.error
    assert len(turns) == 5
    return controller, run, turns


async def request_repair(factory, controller, run, **overrides):
    info = await sync_to_async(review)(run)
    payload = {"expected_digest": info["digest"], "instructions": "Fix the failing build check.",
               "paths": "repaired.txt", **overrides}
    result = await controller.control_run(
        run.pk, "product_repair", payload, user_id=factory.user.pk,
        idempotency_key=f"test-repair:{uuid.uuid4()}",
    )
    assert result[0]


@pytest.mark.parametrize("dirty", [False, True])
async def test_repair_runs_one_agent_then_fresh_checks_preserving_contract_and_history(
    factory, monkeypatch, dirty,
):
    controller, run, turns = await stopped_build(factory, monkeypatch, dirty=dirty)
    try:
        snapshots = copy.deepcopy((run.execution_snapshot, run.product_snapshot))
        previous = [row async for row in run.node_runs.values("pk", "finished_at", "output")]
        validations = [row.pk async for row in run.validations.all()]
        await request_repair(factory, controller, run)
        await dispatch(controller, run)
        assert run.status == "succeeded", run.error
        assert run.phase == "CandidateChecksPassed"
        assert turns == [
            "planner", "implementer", "implementer", "integrator", "verifier", "repair",
        ]
        assert (run.execution_snapshot, run.product_snapshot) == snapshots
        assert run.total_tokens == 18
        assert [row async for row in run.node_runs.filter(
            pk__in=[r["pk"] for r in previous]
        ).values("pk", "finished_at", "output")] == previous
        assert [v.pk async for v in run.validations.filter(pk__in=validations)] == validations
        candidate = await RunCheckpoint.objects.aget(run=run, kind="product_candidate")
        assert candidate.payload["validation_record_id"] not in validations
        assert await run.validations.filter(status="passed").acount() == 1
        assert candidate.payload["repairs"] == await sync_to_async(evidence)(run)
        assert candidate.payload["repairs"][-1]["status"] == "succeeded"
        assert candidate.payload["source_sha"] == await inspect_repository(Path(run.workspace_path))
        await check_candidate(run.pk, candidate.payload["source_sha"])
        with pytest.raises(CodexError, match="completed repair commit"):
            await check_candidate(run.pk, "f" * 40)
        with pytest.raises(IntakeConflict, match="stopped run"):
            await sync_to_async(review)(run)
    finally:
        await controller.tracker.close()


@pytest.mark.parametrize("behavior", ["outside", "space", "interrupt", "forge"])
async def test_failed_or_interrupted_repair_is_retained_and_not_automatically_replayed(
    factory, monkeypatch, behavior,
):
    controller, run, turns = await stopped_build(factory, monkeypatch, behavior=behavior)
    try:
        await request_repair(factory, controller, run)
        await dispatch(controller, run)
        # A cancelled worker may first enter scheduler recovery; it must never repeat the turn.
        assert run.status in {"failed", "cancelled", "retry_scheduled"}, run.error
        assert not await run.checkpoints.filter(kind="product_candidate").aexists()
        assert await run.validations.filter(status="passed").acount() == 0
        repair = await run.node_runs.aget(name="Build repair 1")
        assert repair.status == "failed" and repair.attempt == 1
        assert repair.total_tokens == 3
        await controller.control_run(run.pk, "retry", {}, user_id=factory.user.pk,
                                     idempotency_key=f"retry:{uuid.uuid4()}")
        await dispatch(controller, run)
        assert run.status == "failed" and "Review a new repair request" in run.error
        assert turns.count("repair") == 1 and run.total_tokens == 18
        await repair.arefresh_from_db()
        assert repair.status == "failed" and repair.attempt == 1
    finally:
        await controller.tracker.close()


async def test_review_fences_changed_source_limits_stale_scope_and_duplicate_requests(
    factory, monkeypatch,
):
    controller, run, _ = await stopped_build(factory, monkeypatch)
    try:
        info = await sync_to_async(review)(run)
        kwargs = dict(user_id=factory.user.pk, idempotency_key="same-repair",
                      expected_digest=info["digest"], instructions="Fix the failing build check.",
                      paths="repaired.txt")
        path = Path(run.workspace_path)
        (path / "base.txt").write_text("changed")
        with pytest.raises(IntakeConflict, match="candidate or failure changed"):
            await sync_to_async(enqueue)(factory.store, run.pk, **kwargs)
        (path / "base.txt").write_text("base\n")
        (path / "base.txt").unlink()
        os.mkfifo(path / "base.txt")
        with pytest.raises(WorkspaceError, match="regular files"):
            await sync_to_async(review)(run)
        (path / "base.txt").unlink()
        (path / "base.txt").write_text("base\n")
        for bad in ["../outside", "/absolute", ".git/config", "src/../x", "src/*", "", "src//x"]:
            with pytest.raises(IntakeConflict):
                await sync_to_async(enqueue)(factory.store, run.pk, **{**kwargs, "paths": bad})
        original = await sync_to_async(enqueue)(factory.store, run.pk, **kwargs)
        replay = await sync_to_async(enqueue)(factory.store, run.pk, **kwargs)
        assert original.pk == replay.pk
        with pytest.raises(IntakeConflict, match="different action"):
            await sync_to_async(enqueue)(factory.store, run.pk,
                                        **{**kwargs, "instructions": "Another different repair"})
        assert await OperatorAction.objects.filter(action="product_repair").acount() == 1
        await AgentRun.objects.filter(pk=run.pk).aupdate(status="failed")
        await run.arefresh_from_db()
        for _ in range(2):
            await request_repair(factory, controller, run)
            await AgentRun.objects.filter(pk=run.pk).aupdate(status="failed")
            await run.arefresh_from_db()
        with pytest.raises(IntakeConflict, match="three repair requests"):
            await sync_to_async(review)(run)
        original.payload["contract"]["assignment"]["instructions"] = "Tampered"
        await original.asave(update_fields=["payload"])
        with pytest.raises(IntakeConflict, match="invalid"):
            await sync_to_async(actions)(run)
    finally:
        await controller.tracker.close()


async def test_repair_view_is_private_scoped_csrf_protected_and_read_only_until_post(
    factory, monkeypatch,
):
    controller, run, _ = await stopped_build(factory, monkeypatch)
    monkeypatch.setattr("tempo_web.product_views.get_orchestrator", lambda: controller)
    url = f"/ideas/{factory.product.pk}/runs/{run.pk}/repair/"
    try:
        client = Client(enforce_csrf_checks=True)
        response = await sync_to_async(client.get)(url)
        assert response.status_code == 302 and response.url.startswith("/login/")
        await sync_to_async(client.force_login)(factory.user)
        response = await sync_to_async(client.get)(url)
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert b"Start agent repair" in response.content
        assert not await OperatorAction.objects.filter(action="product_repair").aexists()
        assert (await sync_to_async(client.post)(url, {})).status_code == 403
        assert (await sync_to_async(client.get)(
            f"/ideas/{factory.product.pk + 999}/runs/{run.pk}/repair/"
        )).status_code == 404
        info = await sync_to_async(review)(run)
        response = await sync_to_async(client.post)(url, {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            "request_key": str(uuid.uuid4()), "expected_digest": info["digest"],
            "instructions": "Fix the failing build check.", "paths": "repaired.txt",
        })
        assert response.status_code == 302
        assert await OperatorAction.objects.filter(action="product_repair").acount() == 1
    finally:
        await controller.tracker.close()


async def test_corrupt_snapshot_and_spent_check_budget_cannot_launch_repair(factory, monkeypatch):
    controller, run, _ = await stopped_build(factory, monkeypatch)
    try:
        await run.validations.all().adelete()
        for _ in range(factory.config.validation.max_attempts_per_run):
            await run.validations.acreate(status="failed", started_at=run.started_at)
        with pytest.raises(IntakeConflict, match="attempt limit"):
            await sync_to_async(review)(run)
        run.product_snapshot_digest = "0" * 64
        with pytest.raises(ConfigError):
            await sync_to_async(review)(run)
    finally:
        await controller.tracker.close()


async def test_source_change_after_queue_stops_before_model_turn(factory, monkeypatch):
    controller, run, turns = await stopped_build(factory, monkeypatch)
    try:
        await request_repair(factory, controller, run)
        (Path(run.workspace_path) / "base.txt").write_text("changed after review")
        await dispatch(controller, run)
        assert run.status == "failed" and "changed before repair" in run.error
        assert "repair" not in turns
        assert not await run.checkpoints.filter(kind="product_candidate").aexists()
    finally:
        await controller.tracker.close()


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_repair_submissions_queue_one_assignment(factory, monkeypatch, same_key):
    if connection.vendor != "postgresql":
        pytest.skip("Row locking requires PostgreSQL")
    controller, run, _ = await stopped_build(factory, monkeypatch)
    try:
        info = await sync_to_async(review)(run)
        barrier = Barrier(2)

        def submit(number):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                try:
                    return enqueue(
                        factory.store, run.pk, user_id=factory.user.pk,
                        idempotency_key=f"concurrent-repair:{0 if same_key else number}",
                        expected_digest=info["digest"], instructions="Fix the failed build check.",
                        paths="repaired.txt",
                    ).pk
                except IntakeConflict:
                    return None
            finally:
                connections.close_all()

        def race():
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit, number) for number in range(2)]
                return [future.result(timeout=20) for future in futures]

        results = await asyncio.to_thread(race)
        assert len(set(result for result in results if result)) == 1
        assert results.count(None) == (0 if same_key else 1)
        assert await OperatorAction.objects.filter(action="product_repair").acount() == 1
    finally:
        await controller.tracker.close()
