import subprocess

import pytest

from tempo.config import HooksConfig, ValidationConfig
from tempo.validation import ProjectValidator, workspace_publication_pending
from tempo.validation_server import run_command, stream_command
from tempo.workspace import WorkspaceManager


def publication_repo(tmp_path):
    workspace = tmp_path / "workspace"
    remote = tmp_path / "remote.git"
    workspace.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "tempo@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Tempo"],
        check=True,
    )
    (workspace / "file.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(workspace), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", "Initial"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "push", "--set-upstream", "origin", "main"],
        check=True,
        capture_output=True,
    )
    return workspace


def commit_publication_update(workspace):
    subprocess.run(["git", "-C", str(workspace), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", "Update"],
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_publication_pending_tracks_dirty_and_unpushed_workspace(tmp_path):
    workspace = publication_repo(tmp_path)

    assert await workspace_publication_pending(workspace) is False
    (workspace / "file.txt").write_text("two\n")
    assert await workspace_publication_pending(workspace) is True
    assert await workspace_publication_pending(workspace, branch_ref_updated=True) is True

    commit_publication_update(workspace)
    assert await workspace_publication_pending(workspace) is True
    assert await workspace_publication_pending(workspace, branch_ref_updated=True) is False


@pytest.mark.asyncio
async def test_project_validator_runs_sequence_captures_output_and_cleanup(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-1")
    events = []
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")

    async def on_event(event):
        events.append(event)

    validator = ProjectValidator(
        ValidationConfig(command_timeout_ms=5000),
        manager,
        on_event,
        {"GITHUB_TOKEN"},
    )
    result = await validator.execute(
        {
            "summary": "repository checks",
            "commands": [
                {"name": "Build", "command": "printf built > artifact"},
                {
                    "name": "Test",
                    "command": (
                        'test "${GITHUB_TOKEN-unset}" = unset && '
                        'test "$(cat artifact)" = built && printf passed'
                    ),
                },
            ],
            "cleanup_command": "printf cleaned > cleanup",
        },
        workspace.path,
    )

    assert result["success"] is True
    assert (workspace.path / "cleanup").read_text() == "cleaned"
    completed = [event for event in events if event["event"] == "validation_completed"][0]
    assert completed["success"] is True
    assert [row["exit_code"] for row in completed["commands"]] == [0, 0, 0]
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.mark.asyncio
async def test_project_validator_stops_after_failure_but_still_cleans_up(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-1")

    async def on_event(_event):
        return None

    validator = ProjectValidator(ValidationConfig(), manager, on_event, set())
    result = await validator.execute(
        {
            "summary": "failure",
            "commands": [
                {"name": "Fail", "command": "printf broken; exit 7"},
                {"name": "Never", "command": "touch should-not-exist"},
            ],
            "cleanup_command": "touch cleaned",
        },
        workspace.path,
    )

    assert result["success"] is False
    assert not (workspace.path / "should-not-exist").exists()
    assert (workspace.path / "cleaned").exists()


@pytest.mark.asyncio
async def test_isolated_runner_command_reports_exit_code_and_output(tmp_path):
    result = await run_command("printf runner; exit 3", tmp_path, 5000, 1000)
    assert result == {"exit_code": 3, "output": "runner"}


@pytest.mark.asyncio
async def test_isolated_runner_streams_output_before_result(tmp_path):
    items = [
        item
        async for item in stream_command(
            "printf streamed-output",
            tmp_path,
            5000,
            1000,
        )
    ]
    assert any(item == {"type": "output", "text": "streamed-output"} for item in items)
    assert items[-1] == {
        "type": "result",
        "exit_code": 0,
        "output": "streamed-output",
    }
