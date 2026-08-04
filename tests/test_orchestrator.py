import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.domain import Issue, NodeExecutionState, RunningEntry, Totals
from tempo.errors import CodexError
from tempo.orchestrator import Orchestrator
from tempo.trackers.memory import MemoryTracker
from tempo.workspace import WorkspaceManager


def workflow(tmp_path: Path) -> Path:
    script = Path(__file__).parent / "fixtures" / "fake_app_server.py"
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        f"""---
tracker:
  kind: memory
  provider:
    issues:
      - id: "1"
        identifier: A-1
        title: First
        state: Todo
        priority: 1
  active_states: [Todo]
  terminal_states: [Done]
polling:
  interval_ms: 60000
workspace:
  root: {tmp_path / "workspaces"}
agent:
  max_concurrent_agents: 1
  max_turns: 1
validation:
  enabled: false
codex:
  command: python {script}
  stall_timeout_ms: 10000
---
Work on {{{{ issue.identifier }}}}.
"""
    )
    return path


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_dispatch_completes_with_explicit_no_change_disposition(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.start()
    for _ in range(100):
        if orchestrator.completed:
            break
        await asyncio.sleep(0.02)
    snapshot = orchestrator.snapshot()
    assert snapshot["completed_count"] == 1
    assert snapshot["retries"] == []
    assert snapshot["totals"]["total_tokens"] == 15
    from tempo_web.models import AgentRun, AgentSession, TrackedIssue

    assert await TrackedIssue.objects.filter(identifier="A-1").acount() == 1
    run = await AgentRun.objects.aget(issue__identifier="A-1")
    assert run.status == AgentRun.Status.SUCCEEDED
    assert run.phase == "NoChangesRequired"
    assert run.total_tokens == 15
    assert await AgentSession.objects.filter(run=run, total_tokens=15).acount() == 1
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_specialist_graph_persists_nodes_and_provider_catalog(tmp_path):
    path = workflow(tmp_path)
    text = path.read_text()
    text = text.replace(
        "validation:\n  enabled: false",
        """agents:
  planner:
    role: planner
    completion: turn
  implementer:
    role: implementer
    completion: publication
workflow:
  name: plan-and-deliver
  nodes:
    - {id: plan, agent: planner}
    - {id: deliver, agent: implementer}
  edges:
    - {from: plan, to: deliver}
validation:
  enabled: false""",
    )
    path.write_text(text)
    orchestrator = Orchestrator(str(path))
    await orchestrator.start()
    for _ in range(150):
        if orchestrator.completed:
            break
        await asyncio.sleep(0.02)
    from tempo_web.models import (
        AgentRuntimeDefinition,
        RunNode,
        WorkflowNodeDefinition,
    )

    nodes = [row async for row in RunNode.objects.order_by("id")]
    assert [(node.node_key, node.status) for node in nodes] == [
        ("plan", RunNode.Status.SUCCEEDED),
        ("deliver", RunNode.Status.SUCCEEDED),
    ]
    assert [node.total_tokens for node in nodes] == [15, 15]
    assert await WorkflowNodeDefinition.objects.acount() == 2
    assert await AgentRuntimeDefinition.objects.filter(name="codex", kind="codex").acount() == 1
    assert orchestrator.snapshot()["totals"]["total_tokens"] == 30
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_unblock_resumes_interrupted_node_with_fresh_token_budget(tmp_path):
    path = workflow(tmp_path)
    source = path.read_text()
    source = source.replace(
        "max_turns: 1",
        "max_turns: 1\n  max_tokens_per_run: 14\n  max_retries: 0",
    ).replace(
        "fake_app_server.py",
        "fake_app_server.py --budget-resume",
    )
    path.write_text(source)
    orchestrator = Orchestrator(str(path))
    await orchestrator.start()

    for _ in range(150):
        if "1" in orchestrator.safety_blocked and "1" not in orchestrator.running:
            break
        await asyncio.sleep(0.02)

    from django.contrib.auth import get_user_model

    from tempo_web.models import AgentRun, RunNode

    run = await AgentRun.objects.aget(issue__identifier="A-1")
    interrupted = await RunNode.objects.aget(run=run)
    assert interrupted.status == RunNode.Status.FAILED
    assert interrupted.thread_id == "thread-test"
    assert interrupted.total_tokens == 15
    assert interrupted.thread_total_tokens == 15
    await RunNode.objects.filter(pk=interrupted.pk).aupdate(total_tokens=10)

    user = await get_user_model().objects.acreate_user(username="unblock-operator")
    applied, message = await orchestrator.control_run(
        run.pk,
        "unblock",
        {},
        user_id=user.pk,
        idempotency_key="unblock-token-budget",
    )
    assert applied is True
    assert message == "run queued"

    for _ in range(200):
        if "1" in orchestrator.completed:
            break
        await asyncio.sleep(0.02)

    run = await AgentRun.objects.aget(pk=run.pk)
    resumed = await RunNode.objects.aget(run=run)
    assert run.status == AgentRun.Status.SUCCEEDED
    assert run.attempt == 1
    assert resumed.status == RunNode.Status.SUCCEEDED
    assert resumed.thread_id == "thread-test"
    assert resumed.total_tokens == 10
    assert resumed.thread_total_tokens == 25
    assert resumed.output["no_change_completed"] is True
    assert resumed.output["resumed"] is True
    assert resumed.output["compacted"] is True
    assert "1" not in orchestrator.safety_blocked
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_external_jsonl_runtime_executes_without_scheduler_changes(tmp_path):
    bridge = Path(__file__).parent / "fixtures" / "fake_external_runtime.py"
    path = tmp_path / "external.md"
    path.write_text(
        f"""---
project:
  organization: acme
  slug: external
tracker:
  kind: memory
  provider:
    issues:
      - {{id: "external-1", identifier: E-1, title: External, state: Todo}}
  active_states: [Todo]
  terminal_states: [Done]
workspace:
  root: {tmp_path / "external-workspaces"}
agent:
  max_turns: 1
validation:
  enabled: false
runtime_providers:
  agents-sdk:
    kind: openai-agents
    command: python {bridge}
agents:
  researcher:
    role: researcher
    runtime: agents-sdk
    completion: turn
workflow:
  require_publication: false
  nodes:
    - {{id: research, agent: researcher}}
---
Research {{{{ issue.identifier }}}}.
"""
    )
    orchestrator = Orchestrator(str(path))
    await orchestrator.start()
    for _ in range(100):
        if orchestrator.completed:
            break
        await asyncio.sleep(0.02)
    from tempo_web.models import AgentRun, RunNode

    run = await AgentRun.objects.aget(issue__identifier="E-1")
    node = await RunNode.objects.aget(run=run)
    assert run.status == AgentRun.Status.SUCCEEDED
    assert run.phase == "WorkflowCompleted"
    assert node.runtime == "agents-sdk"
    assert node.output["summary"] == "External specialist finished its assignment."
    await orchestrator.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_database_workflow_configuration_is_seeded_and_reloaded_live(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    _, file_config = await orchestrator.store.initialize()
    await orchestrator._apply_config(file_config)
    assert orchestrator.persistence is not None

    sections, updated_at = await orchestrator.persistence.workflow_configuration()
    assert set(sections) == {
        "runtime_providers",
        "model_providers",
        "tool_providers",
        "agents",
        "workflow",
    }
    orchestrator._workflow_config_updated_at = updated_at

    from tempo_web.models import WorkflowConfiguration, WorkflowNodeDefinition

    row = await WorkflowConfiguration.objects.aget()
    changed = deepcopy(row.configuration)
    changed["agents"]["reviewer"] = {
        "role": "reviewer",
        "runtime": "codex",
        "model": "default",
        "tool_providers": ["tempo"],
        "completion": "turn",
    }
    changed["workflow"] = {
        "name": "live-review",
        "require_publication": False,
        "nodes": [{"id": "review", "agent": "reviewer"}],
        "edges": [],
    }
    row.configuration = changed
    row.revision += 1
    await row.asave(update_fields=["configuration", "revision", "updated_at"])

    assert await orchestrator._reload_database_workflow_if_changed() is True
    _, effective = orchestrator.store.current()
    assert effective.workflow.name == "live-review"
    assert effective.workflow.nodes[0].agent == "reviewer"
    assert await WorkflowNodeDefinition.objects.filter(key="review").aexists()
    await orchestrator.stop()


def test_sort_order():
    newest = Issue(id="1", identifier="B", title="B", state="Todo", priority=2)
    highest = Issue(id="2", identifier="Z", title="Z", state="Todo", priority=1)
    unknown = Issue(id="3", identifier="A", title="A", state="Todo", priority=99)
    assert sorted([newest, unknown, highest], key=Orchestrator._sort_key) == [
        highest,
        newest,
        unknown,
    ]


@pytest.mark.asyncio
async def test_snapshot_combines_durable_and_active_runtime_totals(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.store.initialize()
    issue = Issue(id="live", identifier="A-LIVE", title="Live task", state="Todo")
    entry = RunningEntry(issue=issue, task=None, attempt=None)
    entry.session.codex_input_tokens = 20
    entry.session.codex_output_tokens = 5
    entry.session.codex_total_tokens = 25
    orchestrator.running[issue.id] = entry
    orchestrator.claimed.add(issue.id)
    orchestrator.totals = Totals(input_tokens=100, output_tokens=50, total_tokens=150)
    orchestrator.completed.update({"done-1", "done-2", "done-3"})

    snapshot = orchestrator.snapshot()

    assert snapshot["totals"]["input_tokens"] == 120
    assert snapshot["totals"]["output_tokens"] == 55
    assert snapshot["totals"]["total_tokens"] == 175
    assert snapshot["claimed_count"] == 1
    assert snapshot["completed_count"] == 3
    assert snapshot["running"][0]["identifier"] == "A-LIVE"


@pytest.mark.asyncio
async def test_runtime_safety_limits_stop_token_and_validation_loops(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.store.initialize()
    issue = Issue(id="safe", identifier="A-SAFE", title="Bounded task", state="Todo")
    entry = RunningEntry(issue=issue, task=None, attempt=None)
    orchestrator.running[issue.id] = entry

    with pytest.raises(CodexError, match="token limit") as token_error:
        await orchestrator._codex_event(
            issue.id,
            {
                "event": "thread/tokenUsage/updated",
                "usage": {"total_tokens": 1_000_001},
            },
        )
    assert token_error.value.category == "token_budget_exceeded"
    assert entry.phase == "SafetyLimitReached"

    entry.session.codex_total_tokens = 0
    entry.phase = "StreamingTurn"
    for _ in range(5):
        await orchestrator._codex_event(
            issue.id,
            {"event": "validation_started", "summary": "tests"},
        )
    with pytest.raises(CodexError, match="maximum validation attempts") as attempt_error:
        await orchestrator._codex_event(
            issue.id,
            {"event": "validation_started", "summary": "one too many"},
        )
    assert attempt_error.value.category == "validation_attempt_limit"
    assert entry.session.validation_attempt_count == 5
    assert entry.phase == "SafetyLimitReached"


@pytest.mark.asyncio
async def test_existing_pull_request_discovery_advances_implementation(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.store.initialize()
    issue = Issue(id="existing-pr", identifier="A-PR", title="Already published", state="Todo")
    entry = RunningEntry(issue=issue, task=None, attempt=None)
    orchestrator.running[issue.id] = entry

    await orchestrator._codex_event(
        issue.id,
        {
            "event": "tool_call_completed",
            "tool": "github_api",
            "success": True,
            "arguments": {
                "method": "GET",
                "path": "/repos/acme/widgets/pulls/17",
            },
            "output": (
                '{"html_url":"https://github.example/acme/widgets/pull/17",'
                '"number":17,"state":"open"}'
            ),
        },
    )

    assert entry.phase == "PullRequestCreated"
    assert entry.session.pull_request_created is True
    assert entry.session.pull_request_number == 17


@pytest.mark.asyncio
async def test_token_limit_automatically_rolls_over_to_a_continuation(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.store.initialize()
    issue = Issue(id="blocked", identifier="A-BLOCK", title="Stop", state="Todo")

    async def exceed_budget():
        raise CodexError("budget reached", category="token_budget_exceeded")

    task = asyncio.create_task(exceed_budget())
    await asyncio.gather(task, return_exceptions=True)
    entry = RunningEntry(issue=issue, task=task, attempt=None)
    entry.phase = "SafetyLimitReached"
    orchestrator.running[issue.id] = entry
    orchestrator.claimed.add(issue.id)

    await orchestrator._worker_finished(issue.id, task)

    assert issue.id not in orchestrator.safety_blocked
    assert issue.id in orchestrator.claimed
    assert orchestrator.retries[issue.id].attempt == 1
    assert orchestrator.retries[issue.id].error == "budget reached"


@pytest.mark.asyncio
async def test_token_limit_blocks_after_retry_budget_is_exhausted(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.store.initialize()
    _, config = orchestrator.store.current()
    issue = Issue(id="blocked", identifier="A-BLOCK", title="Stop", state="Todo")

    async def exceed_budget():
        raise CodexError("budget reached", category="token_budget_exceeded")

    task = asyncio.create_task(exceed_budget())
    await asyncio.gather(task, return_exceptions=True)
    entry = RunningEntry(
        issue=issue,
        task=task,
        attempt=config.agent.max_retries,
    )
    entry.phase = "SafetyLimitReached"
    orchestrator.running[issue.id] = entry
    orchestrator.claimed.add(issue.id)

    await orchestrator._worker_finished(issue.id, task)

    assert issue.id in orchestrator.safety_blocked
    assert issue.id not in orchestrator.claimed
    assert orchestrator.retries == {}


@pytest.mark.asyncio
async def test_review_agent_uses_separate_session_and_applies_merge_policy(tmp_path, monkeypatch):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.store.initialize()
    _, config = orchestrator.store.current()
    config.review.enabled = True
    config.review.auto_merge = True
    issue = Issue(id="review", identifier="A-REVIEW", title="Review it", state="Todo")
    entry = RunningEntry(issue=issue, task=None, attempt=None)
    entry.session.pull_request_url = "https://github.test/pull/7"
    entry.session.pull_request_number = 7
    entry.session.codex_input_tokens = 10
    entry.session.codex_output_tokens = 5
    entry.session.codex_total_tokens = 15
    entry.session.turn_count = 1
    orchestrator.running[issue.id] = entry
    manager = WorkspaceManager(config.workspace.root, config.hooks)
    workspace = await manager.create(issue.identifier)
    tracker = MemoryTracker()
    roles = []

    class FakeReviewClient:
        def __init__(self, _config, _manager, _tracker, on_event, approval_callback=None):
            self.on_event = on_event

        async def start_session(self, _workspace, *, role="implementation"):
            roles.append(role)
            return SimpleNamespace(review_decision=None, review_summary=None)

        async def run_turn(self, session, _prompt, _issue):
            session.review_decision = "approve"
            session.review_summary = "Reviewed the diff and validation evidence."
            await self.on_event(
                {
                    "event": "review_completed",
                    "decision": "approve",
                    "summary": session.review_summary,
                }
            )

        async def stop_session(self, _session):
            return None

    monkeypatch.setattr("tempo.orchestrator.CodexAppServer", FakeReviewClient)

    async def approve(_kind, _payload):
        return {"approved": True}

    await orchestrator._run_review_agent(
        issue,
        workspace.path,
        config,
        manager,
        tracker,
        7,
        approve,
    )

    assert roles == ["review"]
    assert entry.phase == "Merged"
    assert entry.session.review_status == "merged"
    assert entry.session.merged is True


def test_completed_publication_node_restores_session_for_retry(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    issue = Issue(id="recover", identifier="A-RECOVER", title="Recover it", state="Todo")
    entry = RunningEntry(issue=issue, task=None, attempt=1)
    entry.graph_nodes["implementation"] = NodeExecutionState(
        node_id="implementation",
        name="Implementation",
        node_type="agent",
        role="implementer",
        status="succeeded",
        output={
            "session_id": "session-1",
            "thread_id": "thread-1",
            "turns": 2,
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "validation_status": "passed",
            "pull_request_url": "https://github.test/acme/repo/pull/17",
            "no_change_completed": False,
            "summary": "Publication completed.",
        },
    )

    orchestrator._restore_completed_node_sessions(entry)
    orchestrator._aggregate_node_sessions(entry)

    assert entry.session.pull_request_created is True
    assert entry.session.pull_request_number == 17
    assert entry.session.pull_request_url == "https://github.test/acme/repo/pull/17"
    assert entry.session.validation_status == "passed"
    assert entry.session.codex_total_tokens == 120


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_completed_review_decision_is_recovered_from_checkpoint(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.start()
    for _ in range(100):
        if orchestrator.completed:
            break
        await asyncio.sleep(0.02)

    from tempo_web.models import AgentRun, RunCheckpoint

    run = await AgentRun.objects.aget(issue__identifier="A-1")
    await RunCheckpoint.objects.acreate(
        run=run,
        sequence=100,
        kind="tool_call_completed",
        idempotency_key="recorded-review-decision",
        payload={
            "tool": "tempo_review",
            "success": True,
            "arguments": {"decision": "approve", "summary": "Review passed."},
        },
    )

    assert await orchestrator.persistence.completed_review_decision(run.pk) == {
        "decision": "approve",
        "summary": "Review passed.",
    }
    await orchestrator.stop()
