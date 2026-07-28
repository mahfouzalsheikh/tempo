from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from tempo.domain import Issue, normalize_state
from tempo.errors import TrackerError

from .base import Tracker
from .memory import MemoryTracker


class GitHubTracker(Tracker):
    """GitHub Issues adapter scoped to one repository."""

    def __init__(
        self,
        *,
        repo: str,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        terminal_states: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        token_env_name: str = "GITHUB_TOKEN",
    ) -> None:
        parsed = urlparse(api_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise TrackerError("GitHub api_url must use HTTPS", category="tracker_config")
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.token = token or os.getenv(token_env_name) or None
        self.token_env_name = token_env_name
        self._publication_authorized: set[str] = set()
        self.terminal_states = {normalize_state(value) for value in terminal_states or ["closed"]}
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tempo-python/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.client = client or httpx.AsyncClient(headers=headers, timeout=20)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def secret_environment_names(self) -> set[str]:
        return {"GITHUB_TOKEN", self.token_env_name}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.client.request(method, f"{self.api_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise TrackerError(str(exc), category="tracker_transport") from exc
        if response.status_code == 429:
            raise TrackerError("GitHub rate limit exceeded", category="tracker_rate_limited")
        if response.status_code >= 400:
            raise TrackerError(
                f"GitHub returned {response.status_code}: {response.text[:500]}",
                category="tracker_response",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TrackerError("GitHub returned invalid JSON", category="tracker_payload") from exc

    async def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        if not states:
            return []
        wanted = {normalize_state(state) for state in states}
        issues: list[Issue] = []
        for state in ("open", "closed"):
            if state not in wanted:
                continue
            page = 1
            while True:
                rows = await self._request(
                    "GET",
                    f"/repos/{self.repo}/issues",
                    params={"state": state, "per_page": 100, "page": page, "sort": "created"},
                )
                if not isinstance(rows, list):
                    raise TrackerError(
                        "GitHub issues payload is not a list",
                        category="tracker_payload",
                    )
                issues.extend(self._normalize(row) for row in rows if self._valid_candidate(row))
                if len(rows) < 100:
                    break
                page += 1
        return issues

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        result: list[Issue] = []
        for issue_id in issue_ids:
            try:
                row = await self._request("GET", f"/repos/{self.repo}/issues/{issue_id}")
            except TrackerError as exc:
                if "404" in str(exc):
                    continue
                raise
            if self._valid_candidate(row):
                result.append(self._normalize(row))
        return result

    @staticmethod
    def _valid_candidate(row: Any) -> bool:
        return (
            isinstance(row, dict)
            and "pull_request" not in row
            and row.get("number") is not None
            and bool(str(row.get("title", "")).strip())
            and bool(str(row.get("state", "")).strip())
        )

    def _normalize(self, row: dict[str, Any]) -> Issue:
        number = str(row["number"])
        labels = [item.get("name", "") for item in row.get("labels", []) if isinstance(item, dict)]
        assignee = row.get("assignee") or {}
        return Issue(
            id=number,
            native_ref={"repo": self.repo, "number": int(number)},
            identifier=f"GH-{number}",
            title=row["title"],
            description=row.get("body"),
            state=row["state"],
            url=row.get("html_url"),
            assignee_id=str(assignee.get("id")) if assignee.get("id") else None,
            labels=labels,
            blocked_by=[],
            dispatchable=True,
            created_at=self._parse_time(row.get("created_at")),
            updated_at=self._parse_time(row.get("updated_at")),
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def agent_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "github_api",
                "description": (
                    "Call the GitHub REST API using the configured repository credential."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "path": {"type": "string"},
                        "params": {"type": "object"},
                        "body": {},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        ]

    def authorize_publication(self, issue_id: str) -> None:
        self._publication_authorized.add(issue_id)

    def revoke_publication(self, issue_id: str) -> None:
        self._publication_authorized.discard(issue_id)

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], issue: Issue
    ) -> dict[str, Any]:
        if name != "github_api":
            return {"success": False, "output": f"Unsupported tool: {name}", "contentItems": []}
        path = arguments.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            return {
                "success": False,
                "output": "path must be a relative API path starting with /",
                "contentItems": [],
            }
        method = str(arguments.get("method", "GET")).upper()
        repo_prefix = f"/repos/{self.repo}/"
        if method not in {"GET", "HEAD"} and not path.startswith(repo_prefix):
            return {
                "success": False,
                "output": "GitHub mutations are restricted to the configured repository.",
                "contentItems": [],
            }
        merge_pattern = rf"^/repos/{re.escape(self.repo)}/pulls/\d+/merge$"
        branch_merge_path = f"/repos/{self.repo}/merges"
        if (method == "PUT" and re.fullmatch(merge_pattern, path)) or (
            method == "POST" and path.rstrip("/") == branch_merge_path
        ):
            return {
                "success": False,
                "output": "Tempo never merges branches or pull requests; human review is required.",
                "contentItems": [],
            }
        if method not in {"GET", "HEAD"} and issue.id not in self._publication_authorized:
            return {
                "success": False,
                "output": (
                    "GitHub writes are locked until local project validation passes. Run the "
                    "project_validation tool first."
                ),
                "contentItems": [],
            }
        try:
            output = await self._request(
                method,
                path,
                params=arguments.get("params"),
                json=arguments.get("body"),
            )
            text = json.dumps(output, ensure_ascii=False)
            return {
                "success": True,
                "output": text,
                "contentItems": [{"type": "inputText", "text": text}],
            }
        except TrackerError as exc:
            text = json.dumps({"error": {"message": str(exc), "category": exc.category}})
            return {
                "success": False,
                "output": text,
                "contentItems": [{"type": "inputText", "text": text}],
            }


def build_tracker(kind: str, provider: dict[str, Any], terminal_states: list[str]) -> Tracker:
    if kind == "memory":
        return MemoryTracker(provider.get("issues") or [])
    token_value = provider.get("token")
    token_env_name = "GITHUB_TOKEN"
    if isinstance(token_value, str) and token_value.startswith("$"):
        token_env_name = token_value[1:]
        token_value = os.getenv(token_env_name)
    return GitHubTracker(
        repo=str(provider["repo"]),
        token=token_value,
        api_url=str(provider.get("api_url", "https://api.github.com")),
        terminal_states=terminal_states,
        token_env_name=token_env_name,
    )
