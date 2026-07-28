import asyncio
from pathlib import Path

import pytest

from tempo.domain import Issue, RunningEntry, Totals
from tempo.errors import CodexError
from tempo.orchestrator import Orchestrator


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
async def test_safety_limit_failure_is_blocked_instead_of_retried(tmp_path):
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

    assert issue.id in orchestrator.safety_blocked
    assert issue.id not in orchestrator.claimed
    assert orchestrator.retries == {}
