import copy
import json
import subprocess
import uuid
from types import SimpleNamespace

import httpx
import pytest

from tempo.domain import Issue
from tempo.errors import LeaseLostError, TrackerError
from tempo.publication import git_command, push_candidate
from tempo.trackers.github import GitHubTracker
from tempo.validation import workspace_fingerprint

pytestmark = pytest.mark.asyncio


def git(path, *args):
    return (
        subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


@pytest.fixture
async def publisher(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    git(workspace, "init", "-b", "main")
    git(workspace, "config", "user.name", "Test")
    git(workspace, "config", "user.email", "test@example.test")
    (workspace / "app.txt").write_text("base")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "base")
    base = git(workspace, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    await git_command(tmp_path, "init", "--bare", str(remote))
    git(workspace, "push", str(remote), "HEAD:refs/heads/main")
    state = {"saved": None, "requests": [], "pr": None, "lost_pr_response": False}

    def head():
        try:
            return git(remote, "rev-parse", "--verify", "refs/heads/tempo/run-7")
        except subprocess.CalledProcessError:
            return None

    def handler(request):
        state["requests"].append(request)
        path = request.url.path
        if path == "/repos/acme/project":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": base}})
        if path.endswith("/git/ref/heads/tempo/run-7"):
            return (
                httpx.Response(200, json={"object": {"sha": head()}})
                if head()
                else httpx.Response(404)
            )
        if path.endswith("/pulls") and request.method == "POST":
            assert state["saved"]["pr_intent"]
            body = json.loads(request.content)
            assert body["head"] == "tempo/run-7" and body["base"] == "main"
            assert body["maintainer_can_modify"] is False
            state["pr"] = {
                "number": 12,
                "html_url": "https://github.test/acme/project/pull/12",
                "state": "open",
                "body": body["body"],
                "head": {"ref": body["head"], "sha": head(), "repo": {"full_name": "acme/project"}},
                "base": {"ref": "main", "repo": {"full_name": "acme/project"}},
            }
            if state["lost_pr_response"]:
                raise httpx.ReadError("lost response", request=request)
            return httpx.Response(201, json=state["pr"])
        if path.endswith("/pulls") or path.endswith("/pulls/12"):
            if state["pr"]:
                state["pr"]["head"]["sha"] = head()
            payload = (
                state["pr"] if path.endswith("/12") else ([state["pr"]] if state["pr"] else [])
            )
            return httpx.Response(200, json=payload)
        raise AssertionError(request.url)

    async def save(value):
        state["saved"] = copy.deepcopy(value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tracker = GitHubTracker(repo="acme/project", client=client)
        tracker.git_remote_url = str(remote)  # Host-owned test transport; never a tool argument.
        await tracker.prepare_publication(workspace, 7, None, save)
        issue = Issue(id="1", identifier="GH-1", title="Task", state="open")
        yield tracker, workspace, remote, state, issue, save


async def candidate(tracker, workspace, issue, content="implemented"):
    (workspace / "app.txt").write_text(content)
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", content)
    fingerprint = await workspace_fingerprint(workspace)
    tracker.authorize_publication(issue.id)
    tracker.accept_validation(fingerprint)
    return git(workspace, "rev-parse", "HEAD"), fingerprint


async def test_publish_pushes_exact_commit_and_recovers_same_pr(publisher):
    tracker, workspace, remote, state, issue, save = publisher
    sha, fingerprint = await candidate(tracker, workspace, issue)
    result = await tracker.publish_candidate(
        {"title": "Implement", "body": "Tested"}, issue, fingerprint
    )
    assert result["success"], result
    assert git(remote, "rev-parse", "refs/heads/tempo/run-7") == sha
    assert git(remote, "rev-parse", "refs/heads/main") == state["saved"]["base_sha"]
    assert json.loads(result["output"])["number"] == 12
    await tracker.prepare_publication(workspace, 7, state["saved"], save)
    retried = await tracker.publish_candidate({"title": "Retry"}, issue, fingerprint)
    assert retried["success"]
    assert sum(request.method == "POST" for request in state["requests"]) == 1
    updated_sha, fingerprint = await candidate(tracker, workspace, issue, "review fix")
    assert (await tracker.publish_candidate({"title": "Review"}, issue, fingerprint))["success"]
    assert git(remote, "rev-parse", "refs/heads/tempo/run-7") == updated_sha
    assert sum(request.method == "POST" for request in state["requests"]) == 1


@pytest.mark.parametrize("boundary", ["push_response", "push_checkpoint", "pr_response"])
async def test_lost_publication_response_is_reconciled(publisher, monkeypatch, boundary):
    tracker, workspace, remote, state, issue, save = publisher
    sha, fingerprint = await candidate(tracker, workspace, issue)
    calls = []

    async def interrupted_push(*args):
        assert state["saved"]["intent"]["status"] == "pending"
        calls.append(args[1])
        await push_candidate(*args)
        if boundary == "push_response":
            raise TrackerError("lost response", category="tracker_transport")
        if boundary == "push_checkpoint":
            raise LeaseLostError("worker died before checkpoint")

    monkeypatch.setattr("tempo.publication.push_candidate", interrupted_push)
    state["lost_pr_response"] = boundary == "pr_response"
    if boundary == "push_checkpoint":
        with pytest.raises(LeaseLostError):
            await tracker.publish_candidate({"title": "Implement"}, issue, fingerprint)
        assert state["saved"]["intent"]["status"] == "pending"
        await tracker.prepare_publication(workspace, 7, state["saved"], save)
    result = await tracker.publish_candidate({"title": "Implement"}, issue, fingerprint)
    assert result["success"], result
    assert calls == [sha]
    assert git(remote, "rev-parse", "refs/heads/tempo/run-7") == sha
    assert sum(request.method == "POST" for request in state["requests"]) == 1


async def test_external_branch_movement_and_dirty_candidate_are_rejected(publisher):
    tracker, workspace, remote, state, issue, _ = publisher
    sha, fingerprint = await candidate(tracker, workspace, issue)
    (workspace / "app.txt").write_text("unvalidated")
    assert not (await tracker.publish_candidate({"title": "Bad"}, issue, fingerprint))["success"]
    git(workspace, "checkout", "--", "app.txt")
    git(workspace, "push", str(remote), f"{sha}:refs/heads/tempo/run-7")
    assert not (await tracker.publish_candidate({"title": "Collision"}, issue, fingerprint))[
        "success"
    ]
    assert state["saved"].get("intent") is None
    assert not any(request.method == "POST" for request in state["requests"])


async def test_expected_sha_prevents_racing_branch_update(publisher):
    tracker, workspace, remote, state, issue, _ = publisher
    sha, _ = await candidate(tracker, workspace, issue)
    git(workspace, "push", str(remote), f"{state['saved']['base_sha']}:refs/heads/tempo/run-7")
    with pytest.raises(TrackerError):
        await push_candidate(
            workspace, sha, "tempo/run-7", None, str(remote), None, tracker.assert_ownership
        )
    assert git(remote, "rev-parse", "refs/heads/tempo/run-7") == state["saved"]["base_sha"]


async def test_no_change_requires_task_base_tree(publisher):
    tracker, workspace, _, _, issue, _ = publisher
    assert await tracker.unchanged_from_base()
    await candidate(tracker, workspace, issue)
    assert not await tracker.unchanged_from_base()
    await candidate(tracker, workspace, issue, "base")
    assert await tracker.unchanged_from_base()
    (workspace / "untracked.txt").write_text("uncommitted")
    assert not await tracker.unchanged_from_base()


@pytest.mark.parametrize(
    "path",
    [
        "/repos/acme/project/../other/issues",
        "/repos/acme/project/issues/../hooks",
        "/repos/acme/project/%2e%2e/other/issues",
        "/repos/acme/project/issues?other=1",
        "/repos/acme/project/issues#fragment",
        "/repos/acme/project/issues\\other",
        "/repos/acme/project//issues",
        "/repos/other/project/issues",
        "/user",
        "/repos/acme/project/actions/secrets",
        "/repos/acme/project/collaborators",
    ],
)
async def test_agent_reads_reject_alternate_paths_and_sensitive_resources(publisher, path):
    tracker, _, _, state, issue, _ = publisher
    count = len(state["requests"])
    result = await tracker.execute_agent_tool("github_api", {"path": path}, issue)
    assert not result["success"]
    assert len(state["requests"]) == count


@pytest.mark.parametrize(
    "path", ["git/refs/heads/main", "git/refs", "contents/app.py", "hooks", "pulls"]
)
async def test_validation_never_unlocks_generic_mutations(publisher, path):
    tracker, workspace, _, state, issue, _ = publisher
    await candidate(tracker, workspace, issue)
    count = len(state["requests"])
    result = await tracker.execute_agent_tool(
        "github_api",
        {
            "method": "PATCH",
            "path": f"/repos/acme/project/{path}",
            "body": {"force": True},
        },
        issue,
    )
    assert not result["success"]
    assert len(state["requests"]) == count


async def test_push_ignores_workspace_push_config_and_hooks(publisher, tmp_path):
    tracker, workspace, remote, _, issue, _ = publisher
    sha, fingerprint = await candidate(tracker, workspace, issue)
    marker = tmp_path / "hook-ran"
    hook = workspace / ".git/hooks/pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    git(workspace, "config", "push.followTags", "true")
    git(workspace, "tag", "unrelated-tag")
    git(workspace, "config", "url.https://invalid.test/.pushInsteadOf", str(remote))
    assert (await tracker.publish_candidate({"title": "Safe"}, issue, fingerprint))["success"]
    assert not marker.exists()
    assert git(remote, "tag") == ""
    assert git(remote, "rev-parse", "refs/heads/tempo/run-7") == sha


async def test_published_event_requires_matching_durable_result(publisher):
    tracker, workspace, _, _, issue, _ = publisher
    _, fingerprint = await candidate(tracker, workspace, issue)
    result = await tracker.publish_candidate({"title": "Publish"}, issue, fingerprint)
    event = {"tool": "github_publish", **result}
    assert tracker.verify_publication_event(event)
    event["output"] = json.dumps({"number": 999, "html_url": "https://unrelated.test/pr/999"})
    assert not tracker.verify_publication_event(event)


@pytest.mark.parametrize("extra", ["sha", "branch", "base", "repo", "force", "draft"])
async def test_publish_rejects_agent_selected_targets(publisher, extra):
    tracker, workspace, _, state, issue, _ = publisher
    _, fingerprint = await candidate(tracker, workspace, issue)
    before = len(state["requests"])
    result = await tracker.publish_candidate(
        {"title": "Bad", extra: "arbitrary"}, issue, fingerprint
    )
    assert not result["success"]
    assert len(state["requests"]) == before


async def test_unvalidated_committed_content_cannot_be_published(publisher):
    tracker, workspace, _, state, issue, _ = publisher
    _, fingerprint = await candidate(tracker, workspace, issue)
    (workspace / "app.txt").write_text("changed after validation")
    git(workspace, "commit", "-am", "unvalidated")
    assert not (await tracker.publish_candidate({"title": "Bad"}, issue, fingerprint))["success"]
    assert not state["saved"].get("intent")


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_index_flags_cannot_hide_a_different_validated_tree(publisher, flag):
    tracker, workspace, _, state, issue, _ = publisher
    git(workspace, "update-index", flag, "app.txt")
    (workspace / "app.txt").write_text("validated contents differ from committed contents")
    assert git(workspace, "status", "--porcelain") == ""
    fingerprint = await workspace_fingerprint(workspace)
    tracker.authorize_publication(issue.id)
    tracker.accept_validation(fingerprint)
    assert not (await tracker.publish_candidate({"title": "Hidden"}, issue, fingerprint))["success"]
    assert not await tracker.unchanged_from_base()
    assert not state["saved"].get("intent")


async def test_executable_mode_changes_invalidate_validation(publisher):
    tracker, workspace, _, state, issue, _ = publisher
    _, fingerprint = await candidate(tracker, workspace, issue)
    (workspace / "app.txt").chmod(0o755)
    git(workspace, "commit", "-am", "change executable mode")
    assert not (await tracker.publish_candidate({"title": "Mode"}, issue, fingerprint))["success"]
    assert not state["saved"].get("intent")


async def test_publish_requires_validation_authority(publisher):
    tracker, workspace, _, state, issue, _ = publisher
    await candidate(tracker, workspace, issue)
    tracker.revoke_publication(issue.id)
    assert not (await tracker.publish_candidate({"title": "Bad"}, issue, None))["success"]
    assert not state["saved"].get("intent")


async def test_pending_intent_cannot_be_replaced_with_a_different_candidate(publisher, monkeypatch):
    tracker, workspace, _, state, issue, _ = publisher
    old_sha, fingerprint = await candidate(tracker, workspace, issue)

    async def failed_push(*args):
        raise TrackerError("unknown transport outcome", category="tracker_transport")

    monkeypatch.setattr("tempo.publication.push_candidate", failed_push)
    assert not (await tracker.publish_candidate({"title": "First"}, issue, fingerprint))["success"]
    _, fingerprint = await candidate(tracker, workspace, issue, "second candidate")
    result = await tracker.publish_candidate({"title": "Second"}, issue, fingerprint)
    assert not result["success"]
    assert state["saved"]["intent"]["sha"] == old_sha


@pytest.mark.parametrize("change", ["marker", "base", "repository", "closed"])
async def test_publication_refuses_an_unrelated_or_closed_pr(publisher, change):
    tracker, workspace, _, state, issue, _ = publisher
    _, fingerprint = await candidate(tracker, workspace, issue)
    assert (await tracker.publish_candidate({"title": "First"}, issue, fingerprint))["success"]
    if change == "marker":
        state["pr"]["body"] = "unrelated"
    elif change == "base":
        state["pr"]["base"]["ref"] = "production"
    elif change == "repository":
        state["pr"]["head"]["repo"]["full_name"] = "other/fork"
    else:
        state["pr"]["state"] = "closed"
    assert not (await tracker.publish_candidate({"title": "Retry"}, issue, fingerprint))["success"]
    assert sum(request.method == "POST" for request in state["requests"]) == 1


async def test_completion_tool_rejects_implemented_delta_even_with_validation_disabled(publisher):
    from tempo.codex import CodexAppServer
    from tempo.config import HooksConfig, ServiceConfig
    from tempo.workspace import WorkspaceManager

    tracker, workspace, _, _, issue, _ = publisher
    await candidate(tracker, workspace, issue)
    responses, events = [], []

    async def on_event(event):
        events.append(event)

    async def send(_session, message):
        responses.append(message)

    config = ServiceConfig.model_validate(
        {
            "tracker": {"kind": "memory", "active_states": ["open"], "terminal_states": ["closed"]},
            "workspace": {"root": workspace.parent},
            "validation": {"enabled": False},
        }
    )
    client = CodexAppServer(
        config,
        WorkspaceManager(workspace.parent, HooksConfig()),
        tracker,
        on_event,
    )
    client._send = send
    session = SimpleNamespace(role="implementation", workspace=workspace)
    await client._handle_server_request(
        session,
        {
            "id": 1,
            "method": "item/tool/call",
            "params": {"tool": "tempo_complete", "arguments": {"reason": "All done"}},
        },
        issue,
    )
    assert not responses[0]["result"]["success"]
    assert not any(event.get("event") == "no_change_completed" for event in events)


@pytest.mark.django_db(transaction=True)
async def test_publication_intent_survives_worker_replacement(publisher):
    from datetime import timedelta

    from tempo.domain import utcnow
    from tempo.persistence import PersistenceStore
    from tempo_web.models import AgentRun

    tracker, workspace, _, state, issue, _ = publisher
    store = PersistenceStore("memory")
    await store.initialize()
    run_id = await store.enqueue_issue(issue)
    token = await store.claim_run(run_id, "first")
    await store.checkpoint(
        run_id, "publication_state", state["saved"],
        idempotency_key=uuid.uuid4().hex, lease_token=token,
    )
    await AgentRun.objects.filter(pk=run_id).aupdate(
        lease_expires_at=utcnow() - timedelta(seconds=1),
    )
    replacement = await store.claim_run(run_id, "replacement")
    assert replacement != token
    assert await store.publication_state(run_id) == state["saved"]
    with pytest.raises(LeaseLostError):
        await store.checkpoint(
            run_id, "publication_state", {"intent": "late"},
            idempotency_key=uuid.uuid4().hex, lease_token=token,
        )
    assert await store.publication_state(run_id) == state["saved"]
