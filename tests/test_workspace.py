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
