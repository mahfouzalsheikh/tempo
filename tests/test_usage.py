import copy
from datetime import timedelta

import pytest

from tempo.domain import Issue, LiveSession, RunningEntry, utcnow
from tempo.orchestrator import Orchestrator
from tempo.persistence import PersistenceStore
from tempo.usage import run_usage
from tempo_web.models import AgentRun, AgentSession, RunNode
from tests.test_orchestrator import workflow


def graph():
    entry = RunningEntry(
        issue=Issue(id="usage", identifier="U-1", title="Parallel usage", state="Todo"),
        task=None, attempt=1,
    )
    entry.node_sessions = {
        "left": LiveSession(codex_input_tokens=90, codex_output_tokens=10, codex_total_tokens=100),
        "right": LiveSession(codex_input_tokens=18, codex_output_tokens=2, codex_total_tokens=20),
    }
    entry.session = entry.node_sessions["right"]
    return entry


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_interleaved_node_reports_keep_run_totals_and_individual_session_usage(tmp_path):
    entry = graph()
    store = PersistenceStore("memory")
    entry.run_record_id = await store.start_run(entry, tmp_path)
    for key, expected in [("left", 100), ("right", 20), ("left", 100)]:
        entry.session = entry.node_sessions[key]
        await store.record_event(entry, {"event": "usage"}, live_session=entry.session)
        run = await AgentRun.objects.aget(pk=entry.run_record_id)
        session = await AgentSession.objects.aget(run=run)
        assert (run.input_tokens, run.output_tokens, run.total_tokens) == (108, 12, 120)
        assert session.total_tokens == expected
    # Cancellation before graph aggregation must retain all observed node usage too.
    await store.finish_run(entry, status="cancelled", error="Stopped")
    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    assert run.total_tokens == 120
    assert [s.codex_total_tokens for s in entry.node_sessions.values()] == [100, 20]


@pytest.mark.asyncio
async def test_live_totals_cover_all_nodes_and_do_not_double_count_aggregate_review(tmp_path):
    controller = Orchestrator(str(workflow(tmp_path)))
    await controller.store.initialize()
    entry = graph()
    controller.running[entry.issue.id] = entry
    for key in ["left", "right"]:
        entry.session = entry.node_sessions[key]
        snapshot = controller.snapshot()
        assert snapshot["totals"]["total_tokens"] == 120
        assert snapshot["running"][0]["session"]["codex_total_tokens"] == 120
    controller._aggregate_node_sessions(entry)
    entry.session.codex_input_tokens += 9
    entry.session.codex_output_tokens += 1
    entry.session.codex_total_tokens += 10
    assert run_usage(entry) == {"input_tokens": 117, "output_tokens": 13, "total_tokens": 130}
    assert controller.snapshot()["totals"]["total_tokens"] == 130
    assert [s.codex_total_tokens for s in entry.node_sessions.values()] == [100, 20]
    single = copy.copy(entry)
    single.node_sessions = {}
    single.node_sessions_aggregated = False
    assert run_usage(single) == run_usage(entry)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_replayed_completion_keeps_its_time_but_a_new_attempt_gets_a_new_time(
    tmp_path, monkeypatch,
):
    entry = graph()
    store = PersistenceStore("memory")
    entry.run_record_id = await store.start_run(entry, tmp_path)
    node = await RunNode.objects.acreate(
        run_id=entry.run_record_id, node_key="left", name="Left", node_type="agent",
    )
    first = utcnow()
    monkeypatch.setattr("tempo.persistence.utcnow", lambda: first)
    await store.finish_run_node(
        entry.run_record_id, "left", status="succeeded", output={"sha": "accepted"},
        lease_token=entry.lease_token,
    )
    later = first + timedelta(seconds=5)
    monkeypatch.setattr("tempo.persistence.utcnow", lambda: later)
    await store.finish_run_node(
        entry.run_record_id, "left", status="succeeded", output={"sha": "accepted"},
        lease_token=entry.lease_token,
    )
    await node.arefresh_from_db()
    assert node.finished_at == first
    assert node.checkpoint["finished_at"] == first.isoformat()
    await store.start_run_node(entry.run_record_id, "left", 2, lease_token=entry.lease_token)
    await store.finish_run_node(
        entry.run_record_id, "left", status="succeeded", output={"sha": "accepted"},
        lease_token=entry.lease_token,
    )
    await node.arefresh_from_db()
    assert node.finished_at == later
