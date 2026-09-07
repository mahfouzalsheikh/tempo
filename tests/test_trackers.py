import json

import httpx
import pytest

from tempo.domain import Issue
from tempo.trackers.github import GitHubTracker
from tempo.trackers.memory import MemoryTracker


@pytest.mark.asyncio
async def test_memory_tracker_empty_short_circuit():
    tracker = MemoryTracker([{"id": "1", "identifier": "A-1", "title": "One", "state": "Todo"}])
    assert await tracker.fetch_issues_by_states([]) == []
    assert await tracker.fetch_issues_by_ids([]) == []


@pytest.mark.asyncio
async def test_github_normalization_and_pr_filtering():
    def handler(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "title": "Fix it",
                        "body": "Description",
                        "state": "open",
                        "labels": [{"name": "Backend"}, {"name": " backend "}],
                        "assignee": {"id": 42},
                        "created_at": "2025-01-01T00:00:00Z",
                        "updated_at": "bad",
                    },
                    {
                        "number": 8,
                        "title": "A PR",
                        "state": "open",
                        "pull_request": {},
                    },
                ],
            )
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(repo="openai/example", client=client)
    issues = await tracker.fetch_issues_by_states(["open"])
    assert len(issues) == 1
    assert issues[0].identifier == "GH-7"
    assert issues[0].labels == ["backend"]
    assert issues[0].updated_at is None
    await client.aclose()


@pytest.mark.asyncio
async def test_github_tool_rejects_absolute_url():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    tracker = GitHubTracker(repo="openai/example", client=client)
    issue = (
        await MemoryTracker(
            [{"id": "1", "identifier": "A-1", "title": "One", "state": "Todo"}]
        ).fetch_issues_by_ids(["1"])
    )[0]
    result = await tracker.execute_agent_tool("github_api", {"path": "https://evil.example"}, issue)
    assert result["success"] is False
    await client.aclose()


@pytest.mark.asyncio
async def test_github_pr_creation_requires_validation_and_agent_merge_is_denied():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"html_url": "https://github.test/pull/1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(repo="openai/example", client=client)
    issue = (
        await MemoryTracker(
            [{"id": "1", "identifier": "A-1", "title": "One", "state": "Todo"}]
        ).fetch_issues_by_ids(["1"])
    )[0]
    create = {"method": "POST", "path": "/repos/openai/example/pulls", "body": {}}

    locked = await tracker.execute_agent_tool("github_api", create, issue)
    assert locked["success"] is False
    assert requests == []

    tracker.authorize_publication(issue.id)
    created = await tracker.execute_agent_tool("github_api", create, issue)
    assert created["success"] is False
    assert "github_publish" in created["output"]
    assert requests == []

    merged = await tracker.execute_agent_tool(
        "github_api",
        {"method": "PUT", "path": "/repos/openai/example/pulls/1/merge"},
        issue,
    )
    assert merged["success"] is False
    assert "independent-review phase" in merged["output"]
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_github_finalization_removes_dispatch_label_and_agent_cannot_edit_issue():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/issues/1") and request.method == "GET":
            return httpx.Response(
                200,
                json={"labels": [{"name": "tempo"}, {"name": "enhancement"}]},
            )
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(
        repo="openai/example",
        client=client,
        required_labels=["tempo"],
    )
    issue = (
        await MemoryTracker(
            [
                {
                    "id": "1",
                    "identifier": "A-1",
                    "title": "One",
                    "state": "open",
                    "labels": ["tempo", "enhancement"],
                }
            ]
        ).fetch_issues_by_ids(["1"])
    )[0]
    tracker.authorize_publication(issue.id)

    denied = await tracker.execute_agent_tool(
        "github_api",
        {
            "method": "PATCH",
            "path": "/repos/openai/example/issues/1",
            "body": {"state": "closed"},
        },
        issue,
    )
    assert denied["success"] is False
    assert requests == []

    await tracker.finalize_pull_request(issue, 7)
    assert [request.method for request in requests] == [
        "GET",
        "POST",
        "GET",
        "POST",
        "GET",
        "PATCH",
    ]
    assert b"Human review required" in requests[1].content
    assert requests[-1].content == b'{"labels":["enhancement"]}'
    await client.aclose()


@pytest.mark.asyncio
async def test_github_review_agent_documents_approval_and_merges():
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/pulls/7") and request.method == "GET":
            return httpx.Response(200, json={"head": {"sha": "a" * 40}})
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/pulls/7/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})
        if path.endswith("/issues/1") and request.method == "GET":
            return httpx.Response(
                200,
                json={"labels": [{"name": "tempo"}, {"name": "enhancement"}]},
            )
        return httpx.Response(201, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(
        repo="openai/example",
        client=client,
        required_labels=["tempo"],
    )
    issue = (
        await MemoryTracker(
            [
                {
                    "id": "1",
                    "identifier": "A-1",
                    "title": "One",
                    "state": "open",
                    "labels": ["tempo", "enhancement"],
                }
            ]
        ).fetch_issues_by_ids(["1"])
    )[0]

    outcome = await tracker.complete_pull_request_review(
        issue,
        7,
        summary="Reviewed the complete diff and validation passed.",
        reviewed_head_sha="a" * 40,
        auto_merge=True,
        merge_method="squash",
        reviewers=[],
        team_reviewers=[],
    )

    assert outcome["status"] == "merged"
    merge_request = next(request for request in requests if request.url.path.endswith("/merge"))
    assert merge_request.method == "PUT"
    assert json.loads(merge_request.content) == {"merge_method": "squash", "sha": "a" * 40}
    assert any(b"independent review agent approved" in request.content for request in requests)
    assert any(b"approved and merged" in request.content for request in requests)
    assert requests[-1].content == b'{"labels":["enhancement"]}'
    await client.aclose()


@pytest.mark.asyncio
async def test_github_merge_block_creates_explicit_human_handoff_and_requests_reviewer():
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/pulls/7") and request.method == "GET":
            return httpx.Response(200, json={"head": {"sha": "a" * 40}})
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/pulls/7/merge"):
            return httpx.Response(405, text="Required approving review missing")
        if path.endswith("/issues/1") and request.method == "GET":
            return httpx.Response(200, json={"labels": [{"name": "tempo"}]})
        return httpx.Response(201, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(
        repo="openai/example",
        client=client,
        required_labels=["tempo"],
    )
    issue = (
        await MemoryTracker(
            [{"id": "1", "identifier": "A-1", "title": "One", "state": "open"}]
        ).fetch_issues_by_ids(["1"])
    )[0]

    outcome = await tracker.complete_pull_request_review(
        issue,
        7,
        summary="The code and tests passed independent review.",
        reviewed_head_sha="a" * 40,
        auto_merge=True,
        merge_method="squash",
        reviewers=["octocat"],
        team_reviewers=["maintainers"],
    )

    assert outcome["status"] == "human_review"
    assert "GitHub did not permit" in outcome["reason"]
    assignment = next(
        request for request in requests if request.url.path.endswith("/requested_reviewers")
    )
    assert assignment.content == b'{"reviewers":["octocat"],"team_reviewers":["maintainers"]}'
    assert sum(b"Human review" in request.content for request in requests) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_github_same_identity_review_is_recorded_without_self_approval():
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/pulls/7") and request.method == "GET":
            return httpx.Response(200, json={"head": {"sha": "a" * 40}})
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/pulls/7/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})
        if path.endswith("/issues/1") and request.method == "GET":
            return httpx.Response(200, json={"labels": [{"name": "tempo"}]})
        return httpx.Response(201, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(
        repo="openai/example",
        token="same-token",
        review_token="same-token",
        client=client,
        required_labels=["tempo"],
    )
    issue = (
        await MemoryTracker(
            [{"id": "1", "identifier": "A-1", "title": "One", "state": "open"}]
        ).fetch_issues_by_ids(["1"])
    )[0]

    outcome = await tracker.complete_pull_request_review(
        issue,
        7,
        summary="Independent review and validation passed.",
        reviewed_head_sha="a" * 40,
        auto_merge=True,
        merge_method="squash",
        reviewers=[],
        team_reviewers=[],
    )

    assert outcome["status"] == "merged"
    assert not any(
        request.method == "POST" and request.url.path.endswith("/pulls/7/reviews")
        for request in requests
    )
    assert any(
        request.method == "POST" and request.url.path.endswith("/issues/7/comments")
        for request in requests
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_github_mutations_are_scoped_to_configured_repository():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    tracker = GitHubTracker(repo="openai/example", client=client)
    issue = (
        await MemoryTracker(
            [{"id": "1", "identifier": "A-1", "title": "One", "state": "Todo"}]
        ).fetch_issues_by_ids(["1"])
    )[0]
    result = await tracker.execute_agent_tool(
        "github_api",
        {"method": "POST", "path": "/repos/other/project/issues", "body": {}},
        issue,
    )
    assert result["success"] is False
    await client.aclose()


@pytest.mark.asyncio
async def test_generic_api_cannot_adopt_an_agent_selected_head_branch():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["head"] == "openai:tempo/issue-1"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 12,
                    "html_url": "https://github.test/pull/12",
                    "head": {"ref": "tempo/issue-1"},
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracker = GitHubTracker(repo="openai/example", client=client)
    issue = (
        await MemoryTracker(
            [{"id": "1", "identifier": "A-1", "title": "One", "state": "Todo"}]
        ).fetch_issues_by_ids(["1"])
    )[0]
    tracker.authorize_publication(issue.id)

    result = await tracker.execute_agent_tool(
        "github_api",
        {
            "method": "POST",
            "path": "/repos/openai/example/pulls",
            "body": {"head": "tempo/issue-1", "base": "main", "title": "Fix"},
        },
        issue,
    )

    assert result["success"] is False
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_status", "merge_count"),
    [
        ("changed_head", "human_review", 0),
        ("missing_head", "human_review", 0),
        ("unavailable_head", "human_review", 0),
        ("legacy_approval", "human_review", 0),
        ("changed_after_check", "human_review", 1),
        ("lost_merge_response", "merged", 1),
        ("lost_response_different_head", "human_review", 1),
        ("already_merged", "merged", 0),
    ],
)
async def test_merge_is_bound_to_reviewed_head_and_reconciles_lost_response(
    scenario, expected_status, merge_count,
):
    requests = []
    sha = "a" * 40
    merged = scenario == "already_merged"

    def handler(request):
        nonlocal merged
        requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls/7"):
            if scenario == "unavailable_head":
                return httpx.Response(503, json={"message": "unavailable"})
            head = "b" * 40 if scenario == "changed_head" or (
                merged and scenario == "lost_response_different_head"
            ) else sha
            return httpx.Response(200, json={
                "head": {} if scenario == "missing_head" else {"sha": head},
                "merged": merged, "merge_commit_sha": "c" * 40 if merged else None,
            })
        if path.endswith("/merge"):
            assert json.loads(request.content)["sha"] == sha
            if scenario == "changed_after_check":
                return httpx.Response(409, json={"message": "Head changed"})
            merged = True
            raise httpx.ReadTimeout("Response lost after merge", request=request)
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tracker = GitHubTracker(repo="openai/example", client=client)
        outcome = await tracker.complete_pull_request_review(
            Issue(id="1", identifier="GH-1", title="Task", state="open"), 7,
            summary="Reviewed candidate.",
            reviewed_head_sha=None if scenario == "legacy_approval" else sha,
            auto_merge=True, merge_method="squash", reviewers=[], team_reviewers=[],
        )
    assert outcome["status"] == expected_status
    assert sum(request.url.path.endswith("/merge") for request in requests) == merge_count
    if merge_count == 0 and expected_status == "human_review":
        assert not any(b"independent review agent approved" in r.content for r in requests)
    if expected_status == "merged":
        assert outcome["sha"] == "c" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_commit", ["a" * 40, "b" * 40])
async def test_formal_approval_is_scoped_and_deduplicated_by_commit(previous_commit):
    sha = "a" * 40
    marker = f"<!-- tempo-review:1:7:{sha} -->"
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": sha}})
        if path.endswith("/reviews") and request.method == "GET":
            return httpx.Response(200, json=[{
                "body": marker, "state": "APPROVED", "commit_id": previous_commit,
            }])
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "c" * 40})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tracker = GitHubTracker(
            repo="openai/example", client=client, token="author", review_token="reviewer",
            review_client=client,
        )
        outcome = await tracker.complete_pull_request_review(
            Issue(id="1", identifier="GH-1", title="Task", state="open"), 7,
            summary="Reviewed candidate.", reviewed_head_sha=sha,
            auto_merge=True, merge_method="squash", reviewers=[], team_reviewers=[],
        )
    assert outcome["status"] == "merged"
    approvals = [r for r in requests if r.method == "POST" and r.url.path.endswith("/reviews")]
    assert len(approvals) == int(previous_commit != sha)
    if approvals:
        assert json.loads(approvals[0].content)["commit_id"] == sha
    await client.aclose()
