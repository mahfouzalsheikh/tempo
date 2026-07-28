import httpx
import pytest

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
async def test_github_pr_creation_requires_validation_and_merge_is_always_denied():
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
    assert created["success"] is True
    assert len(requests) == 1
    assert b"Closes #1" in requests[0].content

    merged = await tracker.execute_agent_tool(
        "github_api",
        {"method": "PUT", "path": "/repos/openai/example/pulls/1/merge"},
        issue,
    )
    assert merged["success"] is False
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_github_finalization_removes_dispatch_label_and_agent_cannot_edit_issue():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
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
    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert requests[-1].content == b'{"labels":["enhancement"]}'
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
