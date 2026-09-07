import asyncio
from pathlib import Path

import pytest

from tempo.config import HooksConfig
from tempo.errors import WorkspaceError
from tempo.workspace import WorkspaceManager, workspace_key


def test_workspace_key_is_collision_resistant():
    assert workspace_key("ABC-1") == "ABC-1"
    assert workspace_key("A/B") != workspace_key("A?B")
    assert "/" not in workspace_key("A/B")


@pytest.mark.asyncio
async def test_create_reuse_hooks_and_remove(tmp_path):
    hooks = HooksConfig(
        after_create="printf created > marker",
        before_run="printf before >> lifecycle",
        after_run="printf after >> lifecycle",
        before_remove="printf remove >> lifecycle",
    )
    manager = WorkspaceManager(tmp_path / "root", hooks)
    first = await manager.create("ABC-1")
    second = await manager.create("ABC-1")
    assert first.created_now is True
    assert second.created_now is False
    assert (first.path / "marker").read_text() == "created"
    await manager.before_run(first.path)
    await manager.after_run(first.path)
    assert (first.path / "lifecycle").read_text() == "beforeafter"
    await manager.remove("ABC-1")
    assert not first.path.exists()


@pytest.mark.asyncio
async def test_fatal_hook_failure_removes_new_workspace(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig(after_create="exit 7"))
    with pytest.raises(WorkspaceError):
        await manager.create("ABC-1")
    assert not manager.path_for("ABC-1").exists()


def test_containment_rejects_escape(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    with pytest.raises(WorkspaceError):
        manager.assert_contained(tmp_path / "outside")


@pytest.mark.asyncio
async def test_cancelled_hook_stops_its_child_processes(tmp_path):
    manager = WorkspaceManager(tmp_path, HooksConfig())
    task = asyncio.create_task(manager.run_hook(
        "before_run", 'sleep 30 & child=$!; printf "%s" "$child" > child.pid; wait',
        tmp_path, fatal=True,
    ))
    pidfile = tmp_path / "child.pid"
    try:
        for _ in range(300):
            try:
                child_pid = int(await asyncio.to_thread(pidfile.read_text))
                break
            except (FileNotFoundError, ValueError):
                await asyncio.sleep(0.01)
        else:
            pytest.fail("Hook child did not start")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    state_file = Path(f"/proc/{child_pid}/stat")
    # A killed child may briefly remain a zombie until its reaper collects it.
    try:
        state = await asyncio.to_thread(state_file.read_text)
    except FileNotFoundError:
        return
    assert state.split()[2] == "Z"
