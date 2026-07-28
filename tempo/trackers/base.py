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

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], issue: Issue
    ) -> dict[str, Any]:
        return {"success": False, "output": f"Unsupported tool: {name}", "contentItems": []}
