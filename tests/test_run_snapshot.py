import asyncio
import copy
import json
from contextvars import Context
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from tempo.config import ServiceConfig
from tempo.domain import Issue, NodeExecutionState, RunningEntry, WorkflowDefinition
from tempo.errors import ConfigError
from tempo.orchestrator import Orchestrator
from tempo.persistence import PersistenceStore
from tempo.run_snapshot import (
    capture_snapshot,
    check_execution_host,
    execution_setting,
    execution_settings,
    portable_model_settings,
    restore_snapshot,
    snapshot_digest,
)
from tempo.workload import execution_backend
from tempo_web.models import AgentRun, RunNode, WorkflowVersion


def configuration(tmp_path):
    return ServiceConfig.model_validate({
        "tracker": {"kind": "memory", "active_states": ["Todo"], "terminal_states": ["Done"],
                    "provider": {"issues": [
                        {"id": "one", "identifier": "A-1", "title": "First", "state": "Todo"},
                        {"id": "two", "identifier": "A-2", "title": "Next", "state": "Todo"},
                    ]}},
        "workspace": {"root": str(tmp_path / "workspaces")},
        "validation": {"enabled": False},
        "agent": {"max_tokens_per_run": 100},
        "agents": {"worker": {"completion": "turn", "prompt": "ORIGINAL ROLE"}},
        "workflow": {"require_publication": False,
                     "nodes": [{"id": "original", "agent": "worker"}]},
    })


def definition(tmp_path):
    return WorkflowDefinition(config={}, prompt_template="ORIGINAL PROMPT {{ issue.title }}",
                              path=tmp_path / "WORKFLOW.md", mtime_ns=0)


def test_snapshots_store_references_and_only_portable_model_settings(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config.codex.environment = {"OPENAI_API_KEY": "$SNAPSHOT_TEST_KEY"}
    monkeypatch.setenv("SNAPSHOT_TEST_KEY", "do-not-persist-this-secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'model="original-model"\n[mcp_servers.private]\nsecret="do-not-import-connectors"\n'
    )
    (tmp_path / "auth.json").write_text('"do-not-import-login"')
    snapshot = capture_snapshot(definition(tmp_path), config)
    encoded = json.dumps(snapshot)
    assert "$SNAPSHOT_TEST_KEY" in encoded
    assert "do-not-" not in encoded
    assert snapshot["execution"]["model_settings"] == {"model": "original-model"}
    loaded, restored = restore_snapshot(snapshot, snapshot_digest(snapshot))
    assert loaded.prompt_template == definition(tmp_path).prompt_template
    assert restored == config
    restored.agents["worker"].prompt = "mutated copy"
    assert snapshot["config"]["agents"]["worker"]["prompt"] == "ORIGINAL ROLE"


@pytest.mark.parametrize("change", ["prompt", "hooks", "limits", "checks", "labels", "image"])
def test_snapshot_identity_covers_more_than_platform_sections(tmp_path, monkeypatch, change):
    config, source = configuration(tmp_path), definition(tmp_path)
    before = snapshot_digest(capture_snapshot(source, config))
    if change == "prompt":
        source = replace(source, prompt_template="Changed prompt")
    elif change == "hooks":
        config.hooks.before_run = "echo changed"
    elif change == "limits":
        config.agent.max_tokens_per_run = 900
    elif change == "checks":
        config.validation.max_attempts_per_run = 2
    elif change == "labels":
        config.tracker.required_labels = ["another"]
    else:
        monkeypatch.setenv("TEMPO_RUNTIME_IMAGE", "sha256:" + "a" * 64)
    assert snapshot_digest(capture_snapshot(source, config)) != before


@pytest.mark.asyncio
async def test_concurrent_execution_defaults_do_not_change_process_environment(
    tmp_path, monkeypatch,
):
    config = configuration(tmp_path)
    monkeypatch.setenv("TEMPO_RUNTIME_IMAGE", "ambient")
    first = capture_snapshot(definition(tmp_path), config)
    second = copy.deepcopy(first)
    first["execution"]["environment"]["TEMPO_RUNTIME_IMAGE"] = "first"
    second["execution"]["environment"]["TEMPO_RUNTIME_IMAGE"] = "second"
    first["execution"]["model_settings"] = {"model": "first"}
    second["execution"]["model_settings"] = {"model": "second"}

    async def observe(snapshot):
        with execution_settings(snapshot["execution"]):
            await asyncio.sleep(0)
            return (execution_setting("TEMPO_RUNTIME_IMAGE"),
                    await asyncio.to_thread(portable_model_settings))

    assert await asyncio.gather(observe(first), observe(second)) == [
        ("first", {"model": "first"}), ("second", {"model": "second"}),
    ]
    assert execution_setting("TEMPO_RUNTIME_IMAGE") == "ambient"


def test_execution_and_validation_defaults_remain_pinned(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    monkeypatch.setenv("TEMPO_RUNTIME_BACKEND", "docker")
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", "sha256:" + "a" * 64)
    old_digest = config.validation.policy_digest
    snapshot = capture_snapshot(definition(tmp_path), config)
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", "sha256:" + "b" * 64)
    with execution_settings(snapshot["execution"]):
        assert execution_backend() == "docker"
        assert config.validation.policy_digest == old_digest
    assert config.validation.policy_digest != old_digest
    monkeypatch.setenv("TEMPO_RUNTIME_BACKEND", "process")
    with pytest.raises(ConfigError, match="backend differs"):
        check_execution_host(snapshot)


@pytest.mark.parametrize("kind", ["missing", "tampered", "unknown_schema"])
def test_missing_or_invalid_snapshots_fail_closed(tmp_path, kind):
    snapshot = capture_snapshot(definition(tmp_path), configuration(tmp_path))
    digest = snapshot_digest(snapshot)
    if kind == "missing":
        snapshot, digest = {}, ""
    elif kind == "tampered":
        snapshot["config"]["validation"]["enabled"] = True
    else:
        snapshot["schema"] = 999
        digest = snapshot_digest(snapshot)
    with pytest.raises(ConfigError) as error:
        restore_snapshot(snapshot, digest)
    assert error.value.category == ("snapshot_missing" if kind == "missing" else "snapshot_invalid")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_run_and_node_definitions_survive_catalog_updates(tmp_path):
    config, source = configuration(tmp_path), definition(tmp_path)
    store = PersistenceStore("memory", config=config, definition=source)
    await store.initialize()
    issue = Issue(id="one", identifier="A-1", title="First", state="Todo")
    run_id = await store.enqueue_issue(issue)
    old = await AgentRun.objects.aget(pk=run_id)
    config.workflow.nodes[0].id = "replacement"
    config.agents["worker"].role = "new-role"
    store.definition = replace(source, prompt_template="NEW PROMPT")
    await store.initialize()
    assert store.workflow_version_id != old.workflow_version_id
    lease = await store.claim_run(run_id, "test")
    bound, restored_source, restored_config, digest = await store.for_execution(run_id)
    assert restored_source.prompt_template.startswith("ORIGINAL PROMPT")
    assert restored_config.workflow.nodes[0].id == "original"
    assert digest == old.snapshot_digest
    assert bound.workflow_version_id == old.workflow_version_id
    entry = RunningEntry(issue=issue, task=None, attempt=0, run_record_id=run_id, lease_token=lease)
    entry.graph_nodes["original"] = NodeExecutionState("original", "Original", "agent")
    # Even a call on the current store must bind node rows to the run's own version.
    await store.initialize_run_nodes(entry)
    node = await RunNode.objects.select_related("node_definition").aget(run_id=run_id)
    assert node.node_key == "original"
    assert node.role == "implementer"
    assert node.node_definition.workflow_version_id == old.workflow_version_id
    # Later edits to the version/catalog do not rewrite a queued run's independent copy.
    await WorkflowVersion.objects.filter(pk=old.workflow_version_id).aupdate(execution_snapshot={})
    assert (await store.for_execution(run_id))[3] == old.snapshot_digest
    await old.arefresh_from_db()
    assert old.execution_snapshot["prompt_template"].startswith("ORIGINAL PROMPT")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_queued_worker_uses_original_prompt_graph_hooks_and_budget_after_reload(
    tmp_path, monkeypatch,
):
    from tempo.agent_runtime import providers

    config = configuration(tmp_path)
    config.hooks.after_create = "echo original > hook-marker"
    source = definition(tmp_path)

    def write_workflow(current, prompt):
        body = yaml.safe_dump(current.model_dump(mode="json", by_alias=True))
        source.path.write_text("---\n" + body + "---\n" + prompt)

    write_workflow(config, source.prompt_template)
    orchestrator = Orchestrator(str(source.path))
    await orchestrator.store.initialize()
    await orchestrator._apply_config(orchestrator.store.current()[1])
    issue = Issue(id="one", identifier="A-1", title="First", state="Todo")
    run_id = await orchestrator.persistence.enqueue_issue(issue)
    original_store = orchestrator.persistence
    config.agents["worker"].prompt = "NEW ROLE"
    config.workflow.nodes[0].id = "replacement"
    config.agent.max_tokens_per_run = 1
    config.hooks.after_create = "echo changed > hook-marker"
    write_workflow(config, "NEW PROMPT")
    await orchestrator.store.initialize()
    # This also simulates controller restart: a new live persistence store/catalog is installed.
    await orchestrator._apply_config(orchestrator.store.current()[1])
    seen = []

    def create_runtime(_config, _profile, _manager, _tracker, on_event, *_args, **_kwargs):
        class Runtime:
            async def start_session(self, workspace, **_kwargs):
                assert (workspace / "hook-marker").read_text().strip() == "original"
                return SimpleNamespace(resumed=False)

            async def run_turn(self, session, prompt, _issue):
                seen.append(prompt)
                await asyncio.create_task(orchestrator.update_platform_config({
                    "agents": {"worker": {"completion": "turn", "prompt": "EXTERNAL RELOAD"}},
                }), context=Context())
                assert orchestrator.persistence.config.agent.max_tokens_per_run == 100
                assert orchestrator.persistence.config.agents["worker"].prompt == "ORIGINAL ROLE"
                await on_event({"event": "turn/completed", "usage": {"total_tokens": 10}})

            async def stop_session(self, _session):
                pass

        return Runtime()

    monkeypatch.setattr(providers, "create_runtime", create_runtime)
    token = await orchestrator.persistence.claim_run(run_id, "test")
    orchestrator._dispatch_locked(issue, 0, run_record_id=run_id, lease_token=token)
    task = orchestrator.running[issue.id].task
    await asyncio.wait_for(task, 10)
    await orchestrator._worker_finished(issue.id, task)
    assert len(seen) == 1 and "ORIGINAL PROMPT" in seen[0] and "ORIGINAL ROLE" in seen[0]
    assert "NEW PROMPT" not in seen[0]
    assert orchestrator.persistence is not original_store
    assert orchestrator.persistence.config.agent.max_tokens_per_run == 1
    node = await RunNode.objects.aget(run_id=run_id)
    assert node.node_key == "original" and node.status == "succeeded"
    current_id = await orchestrator.persistence.enqueue_issue(
        Issue(id="two", identifier="A-2", title="Next", state="Todo"),
    )
    current = await AgentRun.objects.aget(pk=current_id)
    assert current.execution_snapshot["prompt_template"] == "NEW PROMPT"
    assert current.execution_snapshot["config"]["agent"]["max_tokens_per_run"] == 1
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_failure_and_retry_keep_original_retry_policy(tmp_path, monkeypatch):
    config, source = configuration(tmp_path), definition(tmp_path)
    source.path.write_text("---\n" + yaml.safe_dump(config.model_dump(mode="json", by_alias=True))
                           + "---\nOriginal")
    orchestrator = Orchestrator(str(source.path))
    await orchestrator.store.initialize()
    await orchestrator._apply_config(orchestrator.store.current()[1])
    issue = Issue(id="one", identifier="A-1", title="First", state="Todo")
    run_id = await orchestrator.persistence.enqueue_issue(issue)
    before = await AgentRun.objects.aget(pk=run_id)
    # Simulate an operator lowering future-run retry limits before this run fails.
    orchestrator.store.config.agent.max_retries = 0
    observed = []

    async def execute(_issue, _attempt):
        entry = orchestrator.running[issue.id]
        observed.append(entry.execution_config.agent.max_retries)
        if len(observed) == 1:
            raise RuntimeError("provider temporarily unavailable")
        entry.phase = "WorkflowCompleted"

    monkeypatch.setattr(orchestrator, "_run_worker_snapshot", execute)
    for attempt in range(2):
        if attempt:
            from tempo.domain import utcnow

            await AgentRun.objects.filter(pk=run_id).aupdate(available_at=utcnow())
        token = await orchestrator.persistence.claim_run(run_id, "test")
        assert token
        orchestrator._dispatch_locked(issue, attempt, run_record_id=run_id, lease_token=token)
        task = orchestrator.running[issue.id].task
        await asyncio.gather(task, return_exceptions=True)
        await orchestrator._worker_finished(issue.id, task)
        if not attempt:
            failed = await AgentRun.objects.aget(pk=run_id)
            assert failed.status == "retry_scheduled"
    after = await AgentRun.objects.aget(pk=run_id)
    assert observed == [config.agent.max_retries] * 2
    assert after.status == "succeeded"
    assert after.snapshot_digest == before.snapshot_digest
    assert after.execution_snapshot == before.execution_snapshot
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["legacy", "tampered", "missing_workspace"])
async def test_invalid_run_stops_before_tracker_or_workspace_launch(tmp_path, monkeypatch, problem):
    config, source = configuration(tmp_path), definition(tmp_path)
    source.path.write_text("---\n" + yaml.safe_dump(config.model_dump(mode="json", by_alias=True))
                           + "---\nCurrent")
    orchestrator = Orchestrator(str(source.path))
    await orchestrator.store.initialize()
    await orchestrator._apply_config(orchestrator.store.current()[1])
    issue = Issue(id="one", identifier="A-1", title="First", state="Todo")
    run_id = await orchestrator.persistence.enqueue_issue(issue)
    if problem == "legacy":
        await AgentRun.objects.filter(pk=run_id).aupdate(execution_snapshot={}, snapshot_digest="")
    elif problem == "tampered":
        await AgentRun.objects.filter(pk=run_id).aupdate(snapshot_digest="f" * 64)
    else:
        await AgentRun.objects.filter(pk=run_id).aupdate(
            workspace_path=str(config.workspace.root / "A-1"),
        )

    def forbidden(*_args):
        pytest.fail("An invalid run attempted to construct a runtime tracker")

    monkeypatch.setattr("tempo.orchestrator.build_tracker", forbidden)
    token = await orchestrator.persistence.claim_run(run_id, "test")
    orchestrator._dispatch_locked(issue, 0, run_record_id=run_id, lease_token=token)
    task = orchestrator.running[issue.id].task
    await asyncio.gather(task, return_exceptions=True)
    await orchestrator._worker_finished(issue.id, task)
    after = await AgentRun.objects.aget(pk=run_id)
    assert after.status == "failed" and after.phase == "SafetyLimitReached"
    assert {
        "legacy": "predates complete execution snapshots",
        "tampered": "snapshot is invalid",
        "missing_workspace": "recorded workspace is missing",
    }[problem] in after.error
    assert not (config.workspace.root / "A-1").exists()
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_concurrent_versions_have_distinct_complete_snapshots(tmp_path):
    config = configuration(tmp_path)
    first = PersistenceStore("memory", config=config, definition=definition(tmp_path))
    await first.initialize()
    second = PersistenceStore("memory", config=config, definition=replace(
        definition(tmp_path), prompt_template="Second controller",
    ))
    third = PersistenceStore("memory", config=config, definition=replace(
        definition(tmp_path), prompt_template="Third controller",
    ))
    await asyncio.gather(second.initialize(), third.initialize())
    versions = [row async for row in WorkflowVersion.objects.order_by("version")]
    assert [row.version for row in versions] == [1, 2, 3]
    assert {row.execution_snapshot["prompt_template"] for row in versions[1:]} == {
        "Second controller", "Third controller",
    }
    assert all(row.checksum == snapshot_digest(row.execution_snapshot) for row in versions)


@pytest.mark.asyncio
async def test_saved_container_run_cannot_fall_back_to_host_validation(tmp_path, monkeypatch):
    from tempo.validation import ProjectValidator
    from tempo.workspace import WorkspaceManager

    config = configuration(tmp_path)
    monkeypatch.setenv("TEMPO_RUNTIME_BACKEND", "docker")
    snapshot = capture_snapshot(definition(tmp_path), config)

    async def event(_):
        pass

    validator = ProjectValidator(
        config.validation, WorkspaceManager(config.workspace.root, config.hooks), event, set(),
    )
    with execution_settings(snapshot["execution"]):
        with pytest.raises(ConfigError, match="local fallback is blocked"):
            await validator._run_local("touch should-not-exist", tmp_path, 1000)
    assert not (tmp_path / "should-not-exist").exists()
