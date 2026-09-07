import asyncio
import copy

import pytest
import yaml

from tempo.config import ServiceConfig, WorkflowNodeConfig
from tempo.errors import LeaseLostError, WorkspaceError
from tempo.integration import ContributionCoordinator, git, inspect_repository
from tempo.orchestrator import Orchestrator
from tempo.workspace import WorkspaceManager


async def commit(path, name, content):
    (path / name).write_text(content)
    await git(path, "add", "--", name)
    await git(path, "commit", "-m", f"Change {name}")
    return (await git(path, "rev-parse", "HEAD")).decode()


async def repository(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    await git(path, "init", "--template=", "--initial-branch=main")
    await commit(path, "base.txt", "base\n")
    return path


async def coordinator(tmp_path):
    workspace = await repository(tmp_path)
    saved = {}

    async def save(state):
        saved[state["node_id"]] = copy.deepcopy(state)

    async def ownership():
        pass

    return ContributionCoordinator(workspace, "run-1", {}, save, ownership), saved


@pytest.mark.asyncio
async def test_parallel_contributors_have_independent_objects_and_integrate_both_changes(tmp_path):
    queue, saved = await coordinator(tmp_path)
    await git(queue.workspace, "config", "remote.origin.url", "https://sentinel@example.invalid/repo")
    (queue.workspace / ".git" / "hooks").mkdir(exist_ok=True)
    (queue.workspace / ".git" / "hooks" / "post-checkout").write_text("host-hook-sentinel")
    left = await queue.prepare("left")
    right = await queue.prepare("right")
    assert queue.states["left"]["base_sha"] == queue.states["right"]["base_sha"]
    assert left != right != queue.workspace
    assert await git(left, "remote") == b""
    assert not (left / ".git" / "hooks" / "post-checkout").exists()
    assert not (left / ".git" / "objects" / "info" / "alternates").exists()
    left_objects = {p.stat().st_ino for p in (left / ".git" / "objects").rglob("*")}
    right_objects = {p.stat().st_ino for p in (right / ".git" / "objects").rglob("*")}
    assert not left_objects & right_objects
    await asyncio.gather(commit(left, "left.txt", "left"), commit(right, "right.txt", "right"))
    assert not (queue.workspace / "left.txt").exists()
    assert not (left / "right.txt").exists()
    results = await asyncio.gather(
        queue.accept("left", {"summary": "Left"}), queue.accept("right", {"summary": "Right"}),
    )
    assert [r["contribution"]["status"] for r in results] == ["integrated", "integrated"]
    assert (queue.workspace / "left.txt").read_text() == "left"
    assert (queue.workspace / "right.txt").read_text() == "right"
    head = await inspect_repository(queue.workspace)
    recovered = ContributionCoordinator(queue.workspace, "run-1", saved, queue.save,
                                        queue.ownership_check)
    assert await recovered.integrate("left") == results[0]
    assert await inspect_repository(queue.workspace) == head


@pytest.mark.asyncio
async def test_independent_controllers_share_cancellable_filesystem_lock(tmp_path):
    queue, saved = await coordinator(tmp_path)
    other = ContributionCoordinator(queue.workspace, "run-1", saved, queue.save,
                                    queue.ownership_check)
    acquired = asyncio.Event()

    async def contender():
        async with other.exclusive():
            acquired.set()

    async with queue.exclusive():
        task = asyncio.create_task(contender())
        await asyncio.sleep(0.1)
        assert not acquired.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.wait_for(contender(), 1)
    assert acquired.is_set()


@pytest.mark.asyncio
async def test_conflict_preserves_canonical_and_candidate_and_can_retry_after_resolution(tmp_path):
    queue, _ = await coordinator(tmp_path)
    left, right = await queue.prepare("left"), await queue.prepare("right")
    await commit(left, "base.txt", "left\n")
    right_sha = await commit(right, "base.txt", "right\n")
    await queue.accept("left", {})
    before = await inspect_repository(queue.workspace)
    with pytest.raises(WorkspaceError, match="conflicts"):
        await queue.accept("right", {})
    assert await inspect_repository(queue.workspace) == before
    assert await inspect_repository(right) == right_sha
    assert queue.states["right"]["status"] == "accepted"
    # An operator resolves in the integration checkout; retry imports the same pinned commit.
    await commit(queue.workspace, "base.txt", "right\n")
    assert (await queue.integrate("right"))["contribution"]["status"] == "integrated"


@pytest.mark.asyncio
async def test_uncommitted_and_rewritten_history_are_rejected(tmp_path):
    queue, _ = await coordinator(tmp_path)
    path = await queue.prepare("node")
    (path / "new.txt").write_text("uncommitted")
    with pytest.raises(WorkspaceError, match="Commit all"):
        await queue.accept("node", {})
    await git(path, "add", ".")
    await git(path, "commit", "--amend", "-m", "rewritten")
    with pytest.raises(WorkspaceError, match="integration failed"):
        await queue.accept("node", {})
    assert not (queue.workspace / "new.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted_status", ["integrating", "integrated"])
async def test_lost_checkpoint_response_replays_exact_merge(tmp_path, interrupted_status):
    queue, saved = await coordinator(tmp_path)
    path = await queue.prepare("node")
    await commit(path, "new.txt", "new")
    save = queue.save

    async def interrupted(state):
        if state["status"] == interrupted_status:
            if interrupted_status == "integrating":
                await save(state)  # Commit succeeded; caller never received acknowledgement.
            raise asyncio.CancelledError
        await save(state)

    queue.save = interrupted
    with pytest.raises(asyncio.CancelledError):
        await queue.accept("node", {"summary": "completed once"})
    recovered = ContributionCoordinator(queue.workspace, "run-1", saved, save,
                                        queue.ownership_check)
    expected = saved["node"]["integrated_sha"]
    result = await recovered.integrate("node")
    assert result["summary"] == "completed once"
    assert await inspect_repository(queue.workspace) == expected
    assert (await recovered.integrate("node")) == result


@pytest.mark.asyncio
async def test_lost_lease_does_not_move_integration_checkout(tmp_path):
    queue, _ = await coordinator(tmp_path)
    path = await queue.prepare("node")
    await commit(path, "new.txt", "new")
    before = await inspect_repository(queue.workspace)
    save = queue.save

    async def revoked():
        raise LeaseLostError("revoked")

    async def revoke_after_intent(state):
        await save(state)
        if state["status"] == "integrating":
            queue.ownership_check = revoked

    queue.save = revoke_after_intent
    with pytest.raises(LeaseLostError):
        await queue.accept("node", {})
    assert await inspect_repository(queue.workspace) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["symlink", "alternates", "filter", "include", "worktree"])
async def test_rejects_agent_controlled_git_redirection(tmp_path, attack):
    queue, _ = await coordinator(tmp_path)
    path = await queue.prepare("node")
    if attack == "symlink":
        config = path / ".git" / "config"
        config.unlink()
        config.symlink_to(queue.workspace / ".git" / "config")
    elif attack == "alternates":
        (path / ".git" / "objects" / "info" / "alternates").write_text("/private/objects")
    else:
        key, value = {
            "filter": ("filter.evil.clean", "touch /tmp/tempo-should-not-run"),
            "include": ("include.path", "/private/config"),
            "worktree": ("core.worktree", str(queue.workspace)),
        }[attack]
        await git(path, "config", key, value)
    with pytest.raises(WorkspaceError):
        await queue.accept("node", {})


def test_only_isolated_nodes_are_scheduled_together():
    nodes = [WorkflowNodeConfig(id=name, agent="worker", workspace=mode) for name, mode in [
        ("left", "isolated"), ("right", "isolated"), ("verify", "integration"),
        ("publish", "integration"), ("another", "isolated"),
    ]]
    assert [[n.id for n in b] for b in Orchestrator._workspace_batches(nodes, 2)] == [
        ["left", "right"], ["verify"], ["publish"], ["another"],
    ]


@pytest.mark.parametrize("completion,allow_all,tools", [
    ("publication", False, []), ("validation", False, []),
    ("turn", True, []), ("turn", False, ["project_validation"]),
    ("turn", False, ["github_publish"]), ("turn", False, ["tempo_complete"]),
])
def test_contributors_cannot_receive_publication_or_combined_validation_tools(
    tmp_path, completion, allow_all, tools,
):
    with pytest.raises(ValueError, match="isolated contributors"):
        ServiceConfig.model_validate({
            "tracker": {"kind": "memory", "active_states": ["Todo"],
                        "terminal_states": ["Done"]},
            "workspace": {"root": str(tmp_path)},
            "tool_providers": {"read": {"allow_all": allow_all, "tools": tools}},
            "agents": {"worker": {"completion": completion, "tool_providers": ["read"]}},
            "workflow": {"nodes": [{"id": "work", "agent": "worker", "workspace": "isolated"}]},
        })


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_runs_private_contributors_then_combined_check_and_persists_evidence(
    tmp_path, monkeypatch,
):
    from tempo_web.models import RunCheckpoint, RunNode

    config = ServiceConfig.model_validate({
        "tracker": {"kind": "memory", "active_states": ["Todo"], "terminal_states": ["Done"],
                    "provider": {"issues": [
                        {"id": "1", "identifier": "project", "title": "Build", "state": "Todo"},
                    ]}},
        "workspace": {"root": str(tmp_path / "workspaces")},
        "validation": {"enabled": False},
        "tool_providers": {"read": {"allow_all": False, "tools": []}},
        "agents": {"worker": {"completion": "turn", "tool_providers": ["read"]}},
        "workflow": {"require_publication": False, "max_parallel_nodes": 2, "nodes": [
            {"id": "left", "agent": "worker", "workspace": "isolated"},
            {"id": "right", "agent": "worker", "workspace": "isolated"},
            {"id": "combined", "agent": "worker"},
        ], "edges": [{"from": "left", "to": "combined"},
                     {"from": "right", "to": "combined"}]},
    })
    manager = WorkspaceManager(config.workspace.root, config.hooks)
    workspace = await manager.create("project")
    await git(workspace.path, "init", "--template=", "--initial-branch=main")
    await commit(workspace.path, "base.txt", "base")
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("---\n" + yaml.safe_dump(config.model_dump(mode="json", by_alias=True))
                             + "---\nImplement the assigned part.")
    orchestrator = Orchestrator(str(workflow_path))
    started, paths = set(), {}
    together = asyncio.Event()

    async def execute(_issue, _attempt, _definition, _config, node, path, *_args):
        paths[node.id] = path
        if node.id == "combined":
            assert path == workspace.path
            assert (path / "left.txt").read_text() == "left"
            assert (path / "right.txt").read_text() == "right"
        else:
            started.add(node.id)
            if len(started) == 2:
                together.set()
            await asyncio.wait_for(together.wait(), 3)
            await commit(path, f"{node.id}.txt", node.id)
        return {"summary": node.id}

    monkeypatch.setattr(orchestrator, "_execute_agent_node", execute)
    await orchestrator.start()
    try:
        for _ in range(750):
            if orchestrator.completed:
                break
            await asyncio.sleep(0.02)
        assert orchestrator.completed
        assert len(set(paths.values())) == 3
        rows = [row async for row in RunNode.objects.order_by("id")]
        assert [row.status for row in rows] == ["succeeded"] * 3
        assert [r.output["contribution"]["status"] for r in rows[:2]] == ["integrated"] * 2
        states = await orchestrator.persistence.contribution_states(rows[0].run_id)
        assert set(states) == {"left", "right"}
        assert all(state["status"] == "integrated" for state in states.values())
        assert await RunCheckpoint.objects.filter(kind="contribution_state").acount() == 8
    finally:
        await orchestrator.stop()
