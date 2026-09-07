import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from tempo.config import HooksConfig, ValidationConfig
from tempo.validation import ProjectValidator
from tempo.workspace import WorkspaceManager

pytestmark = pytest.mark.asyncio


def policy(**kwargs):
    return ValidationConfig(
        required_checks=[
            {"id": "unit", "name": "Required tests", "command": "printf required"},
        ],
        **kwargs,
    )


@pytest.fixture
async def setup_validator(tmp_path):
    manager = WorkspaceManager(tmp_path, HooksConfig())
    workspace = (await manager.create("task")).path
    events = []

    async def on_event(event):
        events.append(event)

    return ProjectValidator(policy(), manager, on_event, set()), workspace, events


async def test_required_checks_run_when_agent_omits_commands(setup_validator):
    validator, workspace, events = setup_validator
    result = await validator.execute({"summary": "Run the required checks"}, workspace)
    assert result["success"]
    assert result["policy_digest"] == validator.config.policy_digest
    assert result["required_check_ids"] == ["unit"]
    assert [(row["check_id"], row["output"]) for row in result["commands"]] == [
        ("unit", "required")
    ]
    assert events[0]["required_check_ids"] == ["unit"]


async def test_agent_named_extra_cannot_replace_a_failing_required_check(setup_validator):
    validator, workspace, events = setup_validator
    validator.config.required_checks[0].command = "exit 9"
    result = await validator.execute(
        {
            "summary": "Only test my way",
            "commands": [{"name": "Required tests", "command": "true"}],
        },
        workspace,
    )
    assert not result["success"]
    assert len(result["commands"]) == 1
    assert result["commands"][0]["check_id"] == "unit"
    assert result["commands"][0]["exit_code"] == 9
    assert events[-1]["success"] is False


async def test_required_checks_precede_extras_and_policy_cleanup_is_last(setup_validator):
    validator, workspace, _ = setup_validator
    validator.config.cleanup_command = "printf policy-cleanup"
    result = await validator.execute(
        {
            "summary": "Complete sequence",
            "commands": [{"name": "Extra", "command": "printf extra"}],
            "cleanup_command": "printf agent-cleanup",
        },
        workspace,
    )
    assert result["success"]
    assert [row["output"] for row in result["commands"]] == [
        "required",
        "extra",
        "agent-cleanup",
        "policy-cleanup",
    ]
    assert [row["check_id"] for row in result["commands"]] == ["unit", None, None, None]


@pytest.mark.parametrize("failing", ["agent", "policy"])
async def test_cleanup_failure_prevents_a_pass(setup_validator, failing):
    validator, workspace, events = setup_validator
    validator.config.cleanup_command = "exit 2" if failing == "policy" else "printf cleaned"
    result = await validator.execute(
        {
            "summary": "Cleanup matters",
            "cleanup_command": "exit 3" if failing == "agent" else "true",
        },
        workspace,
    )
    assert not result["success"]
    assert result["commands"][-1]["name"] == "Policy cleanup"
    assert not events[-1]["success"]


async def test_agent_cleanup_exception_still_runs_policy_cleanup(setup_validator, monkeypatch):
    validator, workspace, _ = setup_validator
    validator.config.cleanup_command = "touch cleanup-ran"
    original = validator._run_command

    async def interrupted(name, *args, **kwargs):
        if name == "Agent cleanup":
            raise RuntimeError("cleanup transport failed")
        return await original(name, *args, **kwargs)

    monkeypatch.setattr(validator, "_run_command", interrupted)
    with pytest.raises(RuntimeError):
        await validator.execute({"summary": "Cleanup", "cleanup_command": "true"}, workspace)
    assert (workspace / "cleanup-ran").exists()


async def test_default_policy_cannot_pass_with_agent_only_checks(setup_validator):
    validator, workspace, events = setup_validator
    validator.config = ValidationConfig()
    result = await validator.execute(
        {
            "summary": "Pretend complete",
            "commands": [{"name": "No-op", "command": "true"}],
        },
        workspace,
    )
    assert not result["success"]
    assert "required_checks" in result["output"]
    assert events == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"summary": "Override", "required_checks": []},
        {"summary": "Override", "policy": "discovered"},
        {"summary": "Override", "commands": [{"name": "Fake", "command": "true", "id": "unit"}]},
        {"summary": "Malformed", "commands": "true"},
        {"summary": "Malformed", "cleanup_command": False},
    ],
)
async def test_tool_arguments_cannot_change_host_policy(setup_validator, arguments):
    validator, workspace, events = setup_validator
    assert not (await validator.execute(arguments, workspace))["success"]
    assert events == []


async def test_policy_change_during_execution_invalidates_result(setup_validator):
    validator, workspace, events = setup_validator

    async def change_policy(event):
        events.append(event)
        if event["event"] == "validation_command_completed":
            validator.config.required_checks[0].command = "printf changed"

    validator.on_event = change_policy
    assert not (await validator.execute({"summary": "Changing policy"}, workspace))["success"]
    assert events[0]["policy_digest"] != validator.config.policy_digest
    assert not events[-1]["success"]


async def test_policy_digest_covers_checks_modes_timeouts_and_runner(monkeypatch):
    config = policy()
    original = config.policy_digest
    assert config.model_copy(deep=True).policy_digest == original
    for key, value in (
        ("policy", "discovered"),
        ("command_timeout_ms", 1000),
        ("cleanup_command", "true"),
        ("enabled", False),
    ):
        changed = config.model_copy(deep=True)
        setattr(changed, key, value)
        assert changed.policy_digest != original
    config.required_checks[0].command = "different"
    assert config.policy_digest != original
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_URL", "http://other-runner")
    assert policy().policy_digest != original


async def test_profile_schema_rejects_duplicate_ids_and_unknown_fields():
    check = {"id": "unit", "name": "Tests", "command": "true"}
    with pytest.raises(ValidationError):
        ValidationConfig(required_checks=[check, check])
    with pytest.raises(ValidationError):
        ValidationConfig(required_checks=[{**check, "command": "  "}])
    with pytest.raises(ValidationError):
        ValidationConfig(required_checks=[{**check, "timeout_ms": 0}])
    with pytest.raises(ValidationError):
        ValidationConfig(required_check=[])


@pytest.mark.parametrize(
    "results",
    [
        [{"type": "result", "exit_code": False, "output": ""}],
        [{"type": "result", "exit_code": "0", "output": ""}],
        [{"type": "result", "exit_code": 0, "output": None}],
        [{"type": "result", "exit_code": 0, "output": ""}] * 2,
        [],
    ],
)
async def test_remote_runner_cannot_pass_with_malformed_results(
    setup_validator, monkeypatch, results
):
    validator, workspace, events = setup_validator
    validator.config.runner_url = "http://test-runner"
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_TOKEN", "test-runner-credential-" + "x" * 32)
    client_class = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="\n".join(json.dumps(row) for row in results),
            )
        )
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(RuntimeError):
        await validator.execute({"summary": "Runner evidence"}, workspace)
    assert not any(event["event"] == "validation_completed" for event in events)


@pytest.mark.django_db(transaction=True)
async def test_policy_and_check_identity_are_persisted_and_latest_failure_wins(tmp_path):
    from tempo.domain import Issue, RunningEntry
    from tempo.orchestrator import Orchestrator
    from tempo.persistence import PersistenceStore
    from tempo_web.models import ValidationAttempt, ValidationCommand

    store = PersistenceStore("memory")
    issue = Issue(id="1", identifier="A-1", title="Validate", state="open")
    entry = RunningEntry(issue=issue, task=None, attempt=1)
    entry.run_record_id = await store.start_run(entry, tmp_path)
    entry.session.active_node_id = "verify"
    orchestrator = Orchestrator(str(tmp_path / "WORKFLOW.md"))
    orchestrator.persistence = store
    orchestrator.running[issue.id] = entry
    # The event handler needs service configuration, independent of a running worker.
    from tempo.config import ServiceConfig

    config = ServiceConfig.model_validate(
        {
            "tracker": {"kind": "memory", "active_states": ["open"], "terminal_states": ["closed"]},
            "workspace": {"root": tmp_path},
        }
    )
    config.validation = policy()
    orchestrator.store = SimpleNamespace(current=lambda: (None, config))

    async def on_event(event):
        await orchestrator._codex_event(issue.id, event)

    validator = ProjectValidator(
        config.validation, WorkspaceManager(tmp_path, HooksConfig()), on_event, set()
    )
    assert (await validator.execute({"summary": "Required"}, tmp_path))["success"]
    await on_event({"event": "validation_fingerprint_recorded", "fingerprint": "f" * 64})
    attempt = await ValidationAttempt.objects.aget(run_id=entry.run_record_id)
    assert attempt.policy_digest == config.validation.policy_digest
    assert attempt.required_check_ids == ["unit"]
    assert (await ValidationCommand.objects.aget(validation=attempt)).check_id == "unit"
    restored = await store.successful_validation_context(entry.run_record_id)
    assert restored["policy_digest"] == config.validation.policy_digest
    validator.config.required_checks[0].command = "false"
    assert not (await validator.execute({"summary": "New failure"}, tmp_path))["success"]
    assert await store.successful_validation_context(entry.run_record_id) is None


@pytest.mark.django_db(transaction=True)
async def test_saved_review_requires_the_current_policy(tmp_path):
    from tempo.domain import Issue, RunningEntry
    from tempo.persistence import PersistenceStore

    store = PersistenceStore("memory")
    entry = RunningEntry(
        issue=Issue(id="1", identifier="A-1", title="Review", state="open"), task=None, attempt=1
    )
    entry.run_record_id = await store.start_run(entry, tmp_path)
    digest = policy().policy_digest
    await store.checkpoint(
        entry.run_record_id,
        "review_completed",
        {
            "decision": "approve",
            "summary": "Passed review",
            "review_head_sha": "a" * 40,
            "validation_policy_digest": digest,
        },
        idempotency_key="review",
    )
    assert (await store.completed_review_decision(entry.run_record_id, policy_digest=digest))[
        "decision"
    ] == "approve"
    assert (
        await store.completed_review_decision(entry.run_record_id, policy_digest="changed") is None
    )


async def test_missing_policy_stops_dispatch_without_spending_agent_retries(tmp_path):
    import asyncio

    from tempo.config import ServiceConfig
    from tempo.domain import Issue, RunningEntry
    from tempo.errors import CodexError
    from tempo.orchestrator import Orchestrator
    from tempo.trackers.memory import MemoryTracker

    config = ServiceConfig.model_validate(
        {
            "tracker": {"kind": "memory", "active_states": ["open"], "terminal_states": ["closed"]},
            "workspace": {"root": tmp_path},
        }
    )
    orchestrator = Orchestrator(str(tmp_path / "WORKFLOW.md"))
    orchestrator.store = SimpleNamespace(current=lambda: (None, config))
    orchestrator.tracker = MemoryTracker()
    orchestrator.workspace = WorkspaceManager(tmp_path / "workspaces", HooksConfig())
    issue = Issue(id="missing", identifier="A-MISSING", title="Missing policy", state="open")
    task = asyncio.create_task(orchestrator._run_worker(issue, 1))
    orchestrator.running[issue.id] = RunningEntry(issue=issue, task=task, attempt=1)
    with pytest.raises(CodexError) as caught:
        await task
    assert caught.value.category == "validation_policy_missing"
    assert not (tmp_path / "workspaces").exists()
    await orchestrator._worker_finished(issue.id, task)
    assert issue.id in orchestrator.safety_blocked
    assert issue.id not in orchestrator.retries
