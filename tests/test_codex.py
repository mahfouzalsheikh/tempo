from pathlib import Path

import pytest

from tempo.codex import CodexAppServer
from tempo.config import HooksConfig, ServiceConfig
from tempo.domain import Issue
from tempo.trackers.memory import MemoryTracker
from tempo.workspace import WorkspaceManager


@pytest.mark.asyncio
async def test_codex_jsonl_lifecycle(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-1")
    events = []

    async def on_event(event):
        events.append(event)

    script = Path(__file__).parent / "fixtures" / "fake_app_server.py"
    client = CodexAppServer(
        ServiceConfig.model_validate(
            {
                "tracker": {
                    "kind": "memory",
                    "active_states": ["Todo"],
                    "terminal_states": ["Done"],
                },
                "workspace": {"root": tmp_path / "root"},
                "validation": {"enabled": False},
                "codex": {"command": f"python {script}"},
            }
        ),
        manager,
        MemoryTracker(),
        on_event,
    )
    session = await client.start_session(workspace.path)
    issue = Issue(id="1", identifier="A-1", title="Task", state="Todo")
    await client.run_turn(session, "Do it", issue)
    await client.stop_session(session)
    assert any(event["event"] == "session_started" for event in events)
    assert any(event.get("usage", {}).get("total_tokens") == 15 for event in events)


@pytest.mark.asyncio
async def test_workspace_fingerprint_changes_with_project_content(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.py"
    source.write_text("first")
    before = await CodexAppServer._workspace_fingerprint(project)
    source.write_text("second")
    after = await CodexAppServer._workspace_fingerprint(project)
    assert before != after
