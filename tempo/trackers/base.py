from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from tempo.domain import Issue


class Tracker(ABC):
    @abstractmethod
    async def fetch_issues_by_states(self, states: list[str]) -> list[Issue]: ...

    @abstractmethod
    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...

    async def close(self) -> None:
        return None

    def secret_environment_names(self) -> set[str]:
        return set()

    def agent_tool_specs(self) -> list[dict[str, Any]]:
        return []

    def authorize_publication(self, issue_id: str) -> None:
        return None

    def revoke_publication(self, issue_id: str) -> None:
        return None

    async def finalize_pull_request(self, issue: Issue, pull_request_number: int) -> None:
        return None

    async def complete_pull_request_review(
        self,
        issue: Issue,
        pull_request_number: int,
        *,
        summary: str,
        auto_merge: bool,
        merge_method: str,
        reviewers: list[str],
        team_reviewers: list[str],
    ) -> dict[str, Any]:
        if auto_merge:
            return {"status": "merged", "summary": summary}
        return {
            "status": "human_review",
            "reason": "Automatic merge is disabled by workflow policy.",
        }

    async def require_human_review(
        self,
        issue: Issue,
        pull_request_number: int,
        *,
        reason: str,
        summary: str = "",
        reviewers: list[str] | None = None,
        team_reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        return {"status": "human_review", "reason": reason, "summary": summary}

    async def finalize_without_changes(self, issue: Issue, reason: str) -> None:
        return None

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], issue: Issue
    ) -> dict[str, Any]:
        return {"success": False, "output": f"Unsupported tool: {name}", "contentItems": []}
