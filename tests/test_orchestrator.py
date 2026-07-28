import asyncio
from pathlib import Path

import pytest

from tempo.domain import Issue
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


@pytest.mark.asyncio
async def test_dispatch_complete_and_continuation_retry(tmp_path):
    orchestrator = Orchestrator(str(workflow(tmp_path)))
    await orchestrator.start()
    for _ in range(100):
        if orchestrator.retries:
            break
        await asyncio.sleep(0.02)
    snapshot = orchestrator.snapshot()
    assert snapshot["completed_count"] == 1
    assert snapshot["retries"][0]["attempt"] == 1
    assert snapshot["totals"]["total_tokens"] == 15
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
