import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.codex import CodexAppServer
from tempo.config import HooksConfig, ServiceConfig
from tempo.domain import Issue
from tempo.errors import CodexError
from tempo.trackers.memory import MemoryTracker
from tempo.workspace import WorkspaceManager


def git(workspace, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=workspace, check=True, capture_output=True, text=True,
    ).stdout.strip()


def initialize_review_repo(workspace):
    git(workspace, "init", "--initial-branch=main")
    git(workspace, "config", "user.name", "Tempo Test")
    git(workspace, "config", "user.email", "test@example.invalid")
    (workspace / "source.py").write_text("value = 1\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "Candidate")
    return git(workspace, "rev-parse", "HEAD")


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
async def test_codex_resumes_thread_with_fresh_attempt_usage(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-RESUME")
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
    session = await client.start_session(
        workspace.path,
        resume_thread_id="thread-existing",
        usage_baseline={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        },
    )
    issue = Issue(id="resume", identifier="A-RESUME", title="Resume", state="Todo")
    await client.run_turn(session, "Continue", issue)
    await client.stop_session(session)

    assert session.resumed is True
    assert session.thread_id == "thread-existing"
    assert any(event["event"] == "thread_resumed" for event in events)
    assert any(event.get("usage", {}).get("total_tokens") == 15 for event in events)
    assert any(event.get("thread_usage", {}).get("total_tokens") == 165 for event in events)


@pytest.mark.asyncio
async def test_codex_reports_provider_usage_limit_without_losing_message(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-QUOTA")

    async def on_event(_event):
        pass

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
                "codex": {"command": f"python {script} --usage-limit"},
            }
        ),
        manager,
        MemoryTracker(),
        on_event,
    )
    session = await client.start_session(workspace.path)
    issue = Issue(id="quota", identifier="A-QUOTA", title="Wait", state="Todo")

    with pytest.raises(CodexError, match="workspace is out of credits") as exc_info:
        await client.run_turn(session, "Continue", issue)
    await client.stop_session(session)

    assert exc_info.value.category == "provider_usage_limit"


def test_resumed_attempt_local_usage_advances_durable_thread_baseline():
    session = SimpleNamespace(
        resumed=True,
        usage_baseline_input=100,
        usage_baseline_output=50,
        usage_baseline_total=150,
        usage_is_cumulative=None,
    )

    event = CodexAppServer._normalize_usage_for_attempt(
        session,
        {
            "event": "thread/tokenUsage/updated",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    )

    assert event["usage"]["total_tokens"] == 15
    assert event["thread_usage"] == {
        "input_tokens": 110,
        "output_tokens": 55,
        "total_tokens": 165,
    }


def test_token_usage_prefers_cumulative_total_over_last_request():
    event = CodexAppServer._event_from_message(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "last": {
                        "inputTokens": 90,
                        "outputTokens": 10,
                        "totalTokens": 100,
                    },
                    "total": {
                        "inputTokens": 900,
                        "outputTokens": 100,
                        "totalTokens": 1000,
                    },
                }
            },
        }
    )

    assert event["usage"] == {
        "input_tokens": 900,
        "output_tokens": 100,
        "total_tokens": 1000,
    }


@pytest.mark.asyncio
async def test_codex_resume_failure_falls_back_with_explicit_state(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-FALLBACK")
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
                "codex": {"command": f"python {script} --reject-resume"},
            }
        ),
        manager,
        MemoryTracker(),
        on_event,
    )
    session = await client.start_session(
        workspace.path,
        resume_thread_id="thread-missing",
    )
    await client.stop_session(session)

    assert session.resumed is False
    assert session.resume_failure
    assert session.thread_id == "thread-test"
    assert any(event["event"] == "thread_resume_failed" for event in events)


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


@pytest.mark.asyncio
async def test_validation_that_modifies_workspace_is_rejected(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-2")
    events = []

    async def on_event(event):
        events.append(event)

    tracker = MemoryTracker()
    client = CodexAppServer(
        ServiceConfig.model_validate(
            {
                "tracker": {
                    "kind": "memory",
                    "active_states": ["Todo"],
                    "terminal_states": ["Done"],
                },
                "workspace": {"root": tmp_path / "root"},
                "validation": {"enabled": True},
            }
        ),
        manager,
        tracker,
        on_event,
    )
    session = SimpleNamespace(workspace=workspace.path, validation_fingerprint=None)
    issue = Issue(id="2", identifier="A-2", title="Task", state="Todo")

    result = await client._execute_project_validation(
        session,
        {
            "summary": "Pretend edit is validation",
            "commands": [{"name": "Edit", "command": "printf changed > changed.txt"}],
        },
        issue,
    )

    assert result["success"] is False
    assert "modified project files" in result["output"]
    assert session.validation_fingerprint is None
    assert any(event["event"] == "validation_invalidated" for event in events)


@pytest.mark.asyncio
async def test_independent_review_agent_records_typed_decision(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("A-3")
    head_sha = initialize_review_repo(workspace.path)
    events = []
    responses = []

    async def on_event(event):
        events.append(event)

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
            }
        ),
        manager,
        MemoryTracker(),
        on_event,
    )
    session = SimpleNamespace(
        role="review",
        workspace=workspace.path,
        validation_fingerprint=None,
        review_decision=None,
        review_summary=None,
    )

    async def capture_send(_session, message):
        responses.append(message)

    client._send = capture_send
    issue = Issue(id="3", identifier="A-3", title="Review", state="Todo")
    await client._handle_server_request(
        session,
        {
            "id": 41,
            "method": "item/tool/call",
            "params": {
                "tool": "tempo_review",
                "arguments": {
                    "decision": "approve",
                    "summary": "Reviewed the diff and relevant tests.",
                },
            },
        },
        issue,
    )

    assert session.review_decision == "approve"
    assert session.review_head_sha == head_sha
    assert responses[0]["result"]["success"] is True
    assert any(event["event"] == "review_completed" for event in events)
    assert CodexAppServer._review_tool_spec()["inputSchema"]["properties"]["decision"]["enum"] == [
        "approve",
        "human_review",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["none", "unstaged", "staged", "untracked", "new_commit", "failed_validation"]
)
async def test_review_approval_requires_the_successfully_validated_commit(tmp_path, change):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = (await manager.create("review")).path
    original_head = initialize_review_repo(workspace)
    events, responses = [], []

    async def on_event(event):
        events.append(event)

    client = CodexAppServer(
        ServiceConfig.model_validate({
            "tracker": {
                "kind": "memory", "active_states": ["open"], "terminal_states": ["closed"],
            },
            "workspace": {"root": manager.root},
            "validation": {"enabled": True},
        }), manager, MemoryTracker(), on_event,
    )
    session = SimpleNamespace(
        role="review", workspace=workspace, validation_fingerprint=None,
        validation_head_sha=None, review_decision=None, review_summary=None, review_head_sha=None,
    )
    issue = Issue(id="review", identifier="review", title="Review", state="open")
    validation = {"summary": "Check candidate", "commands": [{"name": "Check", "command": "true"}]}
    assert (await client._execute_project_validation(session, validation, issue))["success"]
    assert session.validation_head_sha == original_head

    if change in {"unstaged", "staged"}:
        (workspace / "source.py").write_text("value = 2\n")
        if change == "staged":
            git(workspace, "add", ".")
    elif change == "untracked":
        (workspace / "new.py").write_text("value = 2\n")
    elif change == "new_commit":
        # Even a new commit with identical contents requires fresh validation.
        git(workspace, "commit", "--allow-empty", "-m", "Different candidate")
    elif change == "failed_validation":
        validation["commands"][0]["command"] = "false"
        assert not (await client._execute_project_validation(session, validation, issue))["success"]

    async def capture_send(_session, message):
        responses.append(message)

    client._send = capture_send
    await client._handle_server_request(session, {
        "id": 1, "method": "item/tool/call", "params": {
            "tool": "tempo_review",
            "arguments": {
                "decision": "approve", "summary": "Checked candidate.",
                "review_head_sha": "forged-agent-value",
            },
        },
    }, issue)
    approved = change == "none"
    assert responses[0]["result"]["success"] is approved
    assert session.review_head_sha == (original_head if approved else None)
    review_events = [event for event in events if event["event"] == "review_completed"]
    assert bool(review_events) is approved
    if approved:
        assert review_events[0]["review_head_sha"] == original_head
        assert events[-1]["review_head_sha"] == original_head
        # An unsuccessful recheck must revoke even an already accepted decision.
        validation["commands"][0]["command"] = "false"
        assert not (await client._execute_project_validation(session, validation, issue))["success"]
        assert session.review_decision is None
        assert session.review_head_sha is None
        invalidations = [event for event in events if event["event"] == "review_invalidated"]
        assert len({event["attempt_id"] for event in invalidations}) == 2


@pytest.mark.asyncio
async def test_review_validation_rejects_commit_changes_during_checks(tmp_path):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = (await manager.create("review")).path
    initialize_review_repo(workspace)
    events = []

    async def on_event(event):
        events.append(event)

    client = CodexAppServer(
        ServiceConfig.model_validate({
            "tracker": {
                "kind": "memory", "active_states": ["open"], "terminal_states": ["closed"],
            },
            "workspace": {"root": manager.root},
        }), manager, MemoryTracker(), on_event,
    )
    session = SimpleNamespace(role="review", workspace=workspace)
    issue = Issue(id="review", identifier="review", title="Review", state="open")
    result = await client._execute_project_validation(session, {
        "summary": "A check that changes HEAD without changing files",
        "commands": [{"name": "Commit", "command": "git commit --allow-empty -m changed"}],
    }, issue)
    assert not result["success"]
    assert session.validation_head_sha is None
    assert session.validation_fingerprint is None
    assert events[-1]["event"] == "validation_invalidated"
