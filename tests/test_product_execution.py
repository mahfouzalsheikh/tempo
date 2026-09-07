import asyncio
import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
import yaml
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client

from tempo.config import ServiceConfig
from tempo.contracts.intake import Brief, starter_plan
from tempo.domain import WorkflowDefinition
from tempo.errors import ConfigError
from tempo.intake import IntakeConflict, approve_plan, create_brief, revise_plan
from tempo.integration import git, inspect_repository
from tempo.orchestrator import Orchestrator
from tempo.persistence import PersistenceStore
from tempo.product_execution import ROLES, compile_product, enqueue_product, restore_product
from tempo.run_snapshot import capture_snapshot, snapshot_digest
from tempo_web.models import AgentRun, RunCheckpoint, ValidationAttempt, WorkflowVersion

pytestmark = pytest.mark.django_db(transaction=True)


def configuration(tmp_path):
    return ServiceConfig.model_validate(
        {
            "tracker": {"kind": "memory", "active_states": ["Todo"], "terminal_states": ["Done"]},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "agent": {"max_tokens_per_run": 1000},
            "agents": {"worker": {"completion": "turn", "prompt": "SAVED ROLE"}},
            "hooks": {
                "after_create": "git init --template= --initial-branch=main\n"
                "git config user.name Tempo\ngit config user.email tempo@localhost\n"
                "echo base > base.txt\necho after-hook > .gitignore\n"
                "git add .\ngit commit -m Base",
                "after_run": "touch after-hook",
            },
            "validation": {
                "enabled": True,
                "policy": "required",
                "required_checks": [
                    {
                        "id": "files",
                        "name": "Integrated files",
                        "command": "test -f left.txt && test -f right.txt && test -f after-hook",
                    },
                ],
            },
            "workflow": {
                "max_parallel_nodes": 2,
                "require_publication": True,
                "nodes": [{"id": "original", "agent": "worker"}],
            },
        }
    )


def definition(tmp_path):
    return WorkflowDefinition(
        config={}, prompt_template="SOURCE PROMPT", path=tmp_path / "WORKFLOW.md", mtime_ns=0
    )


def specification():
    return {
        "title": "Literal {{ 7*7 }} product",
        "goal": "Two independent features",
        "users": "Operators",
        "scope": "Left and right",
        "target": "Preview",
        "criteria": [
            {"id": "AC-1", "outcome": "Left exists", "verification": "Check left"},
            {"id": "AC-2", "outcome": "Right exists", "verification": "Check right"},
        ],
    }


def context(tmp_path):
    brief = Brief.model_validate(specification())
    source = capture_snapshot(definition(tmp_path), configuration(tmp_path))
    return {
        "schema": 1,
        "mode": "candidate",
        "plan_id": 1,
        "brief_id": 1,
        "brief": brief.model_dump(mode="json"),
        "plan": starter_plan(brief).model_dump(mode="json"),
        "source_snapshot": source,
        "source_digest": snapshot_digest(source),
        "bindings": dict.fromkeys(ROLES, "worker"),
        "parallelism": 2,
    }


@pytest.fixture
def factory(tmp_path, transactional_db, monkeypatch):
    monkeypatch.setenv("TEMPO_RUNTIME_BACKEND", "process")
    monkeypatch.delenv("TEMPO_VALIDATION_RUNNER_URL", raising=False)
    config, source = configuration(tmp_path), definition(tmp_path)
    store = PersistenceStore("memory", config=config, definition=source)
    async_to_sync(store.initialize)()
    user = get_user_model().objects.create_user("builder", password="test-password")
    product = create_brief(
        project_id=store.project_id,
        specification=specification(),
        user_id=user.pk,
        request_key=uuid.uuid4(),
    )
    plan = product.revisions.first().plans.first()
    approve_plan(product.pk, expected_plan_id=plan.pk, expected_digest=plan.digest, user_id=user.pk)
    plan.refresh_from_db()
    return SimpleNamespace(
        store=store, user=user, product=product, plan=plan, config=config, source=source
    )


def arguments(factory):
    return {
        "expected_plan_id": factory.plan.pk,
        "expected_plan_digest": factory.plan.digest,
        "expected_configuration_digest": snapshot_digest(factory.store.execution_snapshot),
        "bindings": dict.fromkeys(ROLES, "worker"),
        "parallelism": 2,
        "user_id": factory.user.pk,
    }


def enqueue(factory, **changes):
    return enqueue_product(factory.store, factory.product.pk, **(arguments(factory) | changes))


def test_compiler_preserves_policies_and_uses_private_contributors(tmp_path):
    saved = context(tmp_path)
    before = copy.deepcopy(saved)
    result = compile_product(saved)
    assert saved == before
    config = ServiceConfig.model_validate(result["config"])
    assert config.validation.required_checks == configuration(tmp_path).validation.required_checks
    assert not config.workflow.require_publication
    assert saved["source_snapshot"]["config"]["workflow"]["require_publication"]
    assert [node.workspace for node in config.workflow.nodes] == [
        "integration",
        "isolated",
        "isolated",
        "integration",
        "integration",
    ]
    assert all(profile.completion == "turn" for profile in config.agents.values())
    assert all(profile.tool_providers == ["product-none"] for profile in config.agents.values())
    assert not config.tool_providers["product-none"].allow_all
    assert config.tool_providers["product-none"].tools == []


@pytest.mark.parametrize(
    "failure",
    ["checks", "hook", "gate", "profile", "parallel", "bool", "schema", "brief", "source"],
)
def test_compiler_rejects_unsupported_or_stale_contracts(tmp_path, failure):
    saved = context(tmp_path)
    raw = saved["source_snapshot"]["config"]
    if failure == "checks":
        raw["validation"]["required_checks"] = []
    elif failure == "hook":
        raw["hooks"]["after_create"] = None
    elif failure == "gate":
        raw["workflow"]["nodes"] = [{"id": "approve", "type": "human_gate"}]
        raw.update(workflow=ServiceConfig.model_validate(raw).workflow.model_dump(mode="json"))
    elif failure == "profile":
        saved["bindings"]["planner"] = "missing"
    elif failure == "parallel":
        saved["parallelism"] = 3
    elif failure == "bool":
        saved["parallelism"] = True
    elif failure == "schema":
        saved["schema"] = 2
    elif failure == "brief":
        saved["brief"]["goal"] = "Unapproved"
    elif failure == "source":
        saved["source_digest"] = "0" * 64
    if failure in {"checks", "hook", "gate"}:
        saved["source_digest"] = snapshot_digest(saved["source_snapshot"])
    with pytest.raises((ValueError, ConfigError)):
        compile_product(saved)


def test_launch_is_idempotent_pinned_and_rejects_changed_settings(factory):
    run = enqueue(factory)
    assert enqueue(factory).pk == run.pk
    assert AgentRun.objects.count() == 1
    assert run.issue.tracker_kind == "product"
    assert run.fresh_workspace_key.startswith("product-")
    with pytest.raises(IntakeConflict, match="different execution settings"):
        enqueue(factory, parallelism=1)
    original = restore_product(run)
    WorkflowVersion.objects.filter(pk=run.workflow_version_id).update(execution_snapshot={})
    assert restore_product(run) == original


@pytest.mark.parametrize("change", ["unapproved", "plan", "digest", "configuration"])
def test_launch_requires_current_approval_and_reviewed_configuration(factory, change):
    kwargs = {}
    if change == "unapproved":
        factory.plan.approved_at = None
        factory.plan.save(update_fields=["approved_at"])
    elif change == "plan":
        kwargs["expected_plan_id"] = 999
    elif change == "digest":
        kwargs["expected_plan_digest"] = "0" * 64
    else:
        kwargs["expected_configuration_digest"] = "0" * 64
    with pytest.raises(IntakeConflict):
        enqueue(factory, **kwargs)
    assert not AgentRun.objects.exists()


def test_active_product_cannot_start_a_second_plan_and_generic_restart_is_blocked(factory):
    run = enqueue(factory)
    new = revise_plan(
        factory.product.pk,
        expected_plan_id=factory.plan.pk,
        specification=factory.plan.specification,
        user_id=factory.user.pk,
    )
    approve_plan(
        factory.product.pk,
        expected_plan_id=new.pk,
        expected_digest=new.digest,
        user_id=factory.user.pk,
    )
    with pytest.raises(IntakeConflict, match="Stop the previous run"):
        enqueue(factory, expected_plan_id=new.pk, expected_plan_digest=new.digest)
    success, reason = async_to_sync(factory.store.restart_run)(
        run.pk,
        user_id=factory.user.pk,
        idempotency_key="restart-product",
        expected_snapshot_digest=run.snapshot_digest,
    )
    assert not success and reason == "revise_and_approve_product_plan_to_change_execution_settings"


@pytest.mark.parametrize("change", ["contract", "digest", "compiled", "identity"])
def test_corrupt_product_execution_fails_closed(factory, change):
    run = enqueue(factory)
    if change == "contract":
        run.product_snapshot["brief"]["goal"] = "Changed"
    elif change == "digest":
        run.product_snapshot_digest = "0" * 64
    elif change == "compiled":
        run.execution_snapshot["prompt_template"] = "Changed"
        run.snapshot_digest = snapshot_digest(run.execution_snapshot)
    else:
        run.execution_plan_id += 1
    with pytest.raises(ConfigError) as error:
        restore_product(run)
    assert error.value.category == "snapshot_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "check",
        "cleanup",
        "history",
        "forged_validation",
        "forged_publication",
        "forged_build",
        "retry",
    ],
)
async def test_product_executes_parallel_contributions_and_host_checks_after_cleanup(
    factory,
    monkeypatch,
    failure,
):
    from tempo.agent_runtime import providers

    source = factory.source
    if failure == "check":
        factory.config.validation.required_checks[0].command = "exit 1"
        factory.config.validation.max_attempts_per_run = 1
    elif failure == "cleanup":
        factory.config.validation.cleanup_command = "echo changed >> base.txt"
    source.path.write_text(
        "---\n"
        + yaml.safe_dump(factory.config.model_dump(mode="json", by_alias=True))
        + "---\nSOURCE PROMPT"
    )
    controller = Orchestrator(str(source.path))
    await controller.store.initialize()
    await controller._apply_config(controller.store.current()[1])
    factory.store = controller.persistence
    run = await sync_to_async(enqueue)(factory)
    old_digest = run.snapshot_digest
    # A catalog reload must not replace this approved graph, role prompt, or check policy.
    await controller.update_platform_config(
        {
            "agents": {"worker": {"completion": "turn", "prompt": "NEW ROLE"}},
        }
    )
    observed, paths, bases = [], [], []
    both_started = asyncio.Event()

    def create_runtime(_config, profile, _manager, tracker, on_event, *_args, **_kwargs):
        assert tracker.__class__.__name__ == "MemoryTracker"

        class Runtime:
            async def start_session(self, workspace, **_kwargs):
                return SimpleNamespace(resumed=False, workspace=workspace)

            async def run_turn(self, session, prompt, _issue):
                observed.append(prompt)
                assert "{{ 7*7 }}" in prompt and "SAVED ROLE" in prompt
                assert "NEW ROLE" not in prompt
                if profile.role == "planner":
                    if failure == "forged_validation":
                        await on_event({"event": "validation_completed", "success": True})
                    elif failure == "forged_build":
                        await on_event({"event": "build_preparation_started"})
                    elif failure == "forged_publication":
                        await on_event(
                            {
                                "event": "tool_call_completed",
                                "tool": "github_publish",
                                "host_publication": True,
                                "success": True,
                            }
                        )
                    elif failure == "retry" and len(observed) == 1:
                        raise RuntimeError("Temporary provider failure")
                if profile.role == "implementer":
                    path = session.workspace
                    paths.append(path)
                    bases.append(await inspect_repository(path))
                    if len(paths) == 2:
                        both_started.set()
                    await asyncio.wait_for(both_started.wait(), 5)
                    filename = (
                        "left.txt"
                        if '"id": "build-1"' in prompt.split("Assigned product task")[-1]
                        else "right.txt"
                    )
                    (path / filename).write_text(filename)
                    await git(path, "add", filename)
                    await git(path, "commit", "-m", filename)
                elif profile.role in {"integrator", "verifier"}:
                    assert (session.workspace / "left.txt").exists()
                    assert (session.workspace / "right.txt").exists()
                if profile.role == "verifier" and failure == "history":
                    await git(session.workspace, "checkout", "--orphan", "rewritten")
                    await git(session.workspace, "commit", "-m", "Unrelated root")
                await on_event({"event": "turn/completed", "usage": {"total_tokens": 3}})

            async def stop_session(self, _session):
                pass

        return Runtime()

    monkeypatch.setattr(providers, "create_runtime", create_runtime)
    issue = await controller.persistence.queued_issue(run.pk)
    token = await controller.persistence.claim_run(run.pk, "test")
    assert token
    controller._dispatch_locked(issue, 0, run_record_id=run.pk, lease_token=token)
    task = controller.running[issue.id].task
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 25)
        await controller._worker_finished(issue.id, task)
        await run.arefresh_from_db()
        if failure == "retry":
            from tempo.domain import utcnow

            assert run.status == "retry_scheduled"
            await AgentRun.objects.filter(pk=run.pk).aupdate(available_at=utcnow())
            token = await controller.persistence.claim_run(run.pk, "retry-worker")
            assert token
            controller._dispatch_locked(issue, 1, run_record_id=run.pk, lease_token=token)
            task = controller.running[issue.id].task
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 25)
            await controller._worker_finished(issue.id, task)
            await run.arefresh_from_db()
        elif failure:
            assert run.status == "failed", run.error
            assert not await RunCheckpoint.objects.filter(
                run=run, kind="product_candidate"
            ).aexists()
            assert not await ValidationAttempt.objects.filter(run=run, status="passed").aexists()
            assert not run.pull_request_url
            if failure == "check":
                assert (
                    await ValidationAttempt.objects.filter(run=run, status="failed").acount() == 1
                )
                success, _ = await controller.control_run(
                    run.pk,
                    "retry",
                    {},
                    user_id=factory.user.pk,
                    idempotency_key="retry-failed-checks",
                )
                assert success
                token = await controller.persistence.claim_run(run.pk, "check-retry")
                assert token
                controller._dispatch_locked(issue, 1, run_record_id=run.pk, lease_token=token)
                task = controller.running[issue.id].task
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 25)
                await controller._worker_finished(issue.id, task)
                await run.arefresh_from_db()
                assert run.status == "failed" and "attempt limit" in run.error
                assert len(observed) == 5
                assert await ValidationAttempt.objects.filter(run=run).acount() == 1
            return
        assert run.status == "succeeded", run.error
        assert run.phase == "CandidateChecksPassed"
        assert run.snapshot_digest == old_digest
        assert len(observed) == (6 if failure == "retry" else 5) and run.total_tokens == 15
        assert paths[0] != paths[1] and bases[0] == bases[1]
        assert await run.node_runs.filter(status="succeeded", node_definition=None).acount() == 5
        candidate = await RunCheckpoint.objects.aget(run=run, kind="product_candidate")
        validation = await ValidationAttempt.objects.aget(
            pk=candidate.payload["validation_record_id"]
        )
        assert validation.status == "passed"
        assert candidate.payload["required_check_ids"] == ["files"]
        assert candidate.payload["workspace_fingerprint"]
        assert candidate.payload["plan_digest"] == factory.plan.digest
        assert not run.pull_request_url
    finally:
        await controller.stop()


def test_execution_pages_require_authentication_and_show_readiness(factory, monkeypatch):
    from tempo_web import product_views

    controller = SimpleNamespace(persistence=factory.store)
    monkeypatch.setattr(product_views, "get_orchestrator", lambda: controller)
    client = Client(enforce_csrf_checks=True)
    base = f"/ideas/{factory.product.pk}"
    assert client.get(base + "/execute/").status_code == 302
    assert client.get(f"/api/v1/briefs/{factory.product.pk}/execution").status_code == 401
    client.force_login(factory.user)
    page = client.get(base + "/execute/")
    assert page.status_code == 200
    assert b"Integrated files" in page.content
    assert b"candidate" in page.content
    assert client.post(base + "/execute/", {}).status_code == 403
    info = client.get(f"/api/v1/briefs/{factory.product.pk}/execution").json()
    assert info["parallel_limit"] == 2 and not info["blocked"]
    run = enqueue(factory)
    assert client.get(base + "/").status_code == 200
    assert client.get(f"/api/v1/briefs/{factory.product.pk}").json()["execution_started"]
    other = create_brief(
        project_id=factory.store.project_id,
        specification=specification(),
        user_id=factory.user.pk,
        request_key=uuid.uuid4(),
    )
    token = client.cookies["csrftoken"].value
    assert (
        client.post(
            f"/ideas/{other.pk}/runs/{run.pk}/cancel/", {"csrfmiddlewaretoken": token}
        ).status_code
        == 404
    )


def test_concurrent_launches_create_one_durable_run(factory):
    if connection.vendor != "postgresql":
        pytest.skip("Row locking requires PostgreSQL")
    barrier = Barrier(2)
    args = arguments(factory)

    def submit():
        connections.close_all()
        try:
            barrier.wait(timeout=5)
            return enqueue_product(factory.store, factory.product.pk, **args).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        ids = [future.result(timeout=15) for future in futures]
    assert ids[0] == ids[1] and AgentRun.objects.count() == 1


def test_execution_api_queues_once_and_wakes_the_controller(factory, monkeypatch):
    from tempo_web import product_views

    wakes = []

    async def refresh():
        wakes.append(True)

    monkeypatch.setattr(
        product_views,
        "get_orchestrator",
        lambda: SimpleNamespace(
            persistence=factory.store,
            refresh=refresh,
        ),
    )
    client = Client()
    client.force_login(factory.user)
    payload = arguments(factory)
    payload.pop("user_id")
    payload["mode"] = "candidate"
    url = f"/api/v1/briefs/{factory.product.pk}/execute"
    for _ in range(2):
        response = client.post(url, payload, content_type="application/json")
        assert response.status_code == 201, response.content
        assert response.json()["runs"][0]["status"] == "queued"
    assert len(wakes) == 2 and AgentRun.objects.count() == 1
    payload["mode"] = "deployment"
    assert client.post(url, payload, content_type="application/json").status_code == 409


def test_plan_edit_and_launch_are_serialized(factory):
    if connection.vendor != "postgresql":
        pytest.skip("Row locking requires PostgreSQL")
    barrier = Barrier(2)
    args = arguments(factory)

    def launch_or_edit(edit):
        connections.close_all()
        try:
            barrier.wait(timeout=5)
            if edit:
                return revise_plan(
                    factory.product.pk,
                    expected_plan_id=factory.plan.pk,
                    specification=factory.plan.specification,
                    user_id=factory.user.pk,
                ).pk
            try:
                return enqueue_product(factory.store, factory.product.pk, **args).pk
            except IntakeConflict:
                return None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        launch = pool.submit(launch_or_edit, False)
        edit = pool.submit(launch_or_edit, True)
        run_id, new_plan_id = launch.result(timeout=15), edit.result(timeout=15)
    assert new_plan_id != factory.plan.pk
    if run_id:
        run = AgentRun.objects.get(pk=run_id)
        assert run.execution_plan_id == factory.plan.pk
        assert restore_product(run)["plan"] == factory.plan.specification
    else:
        assert not AgentRun.objects.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote,accepted",
    [
        ("https://github.com/team/project.git", True),
        ("git@github.com:team/project.git", True),
        ("https://github.com/other/project.git", False),
        ("https://example.invalid/team/project.git", False),
        ("https://token@github.com/team/project.git", False),
        ("http://github.com/team/project.git", False),
    ],
)
async def test_repository_origin_must_match_project_without_embedded_credentials(
    factory,
    tmp_path,
    remote,
    accepted,
):
    from tempo.product_execution import prepare_product_repository

    run = await sync_to_async(enqueue)(factory)
    path = tmp_path / "repository"
    path.mkdir()
    await git(path, "init", "--template=", "--initial-branch=main")
    (path / "base.txt").write_text("Base")
    await git(path, "add", ".")
    await git(path, "commit", "-m", "Base")
    await git(path, "remote", "add", "origin", remote)
    factory.config.tracker.kind = "github"
    factory.config.tracker.provider = {"repo": "team/project"}
    entry = SimpleNamespace(
        execution_config=factory.config, run_record_id=run.pk, lease_token="test"
    )
    checkpoints = []

    async def checkpoint(*args, **kwargs):
        checkpoints.append(args)

    store = SimpleNamespace(checkpoint=checkpoint)
    if accepted:
        await prepare_product_repository(entry, path, store)
        assert checkpoints[0][1] == "product_base"
    else:
        with pytest.raises(IntakeConflict, match="does not match"):
            await prepare_product_repository(entry, path, store)
        assert not checkpoints
