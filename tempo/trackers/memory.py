from __future__ import annotations

from typing import Any

from tempo.domain import Issue, normalize_state

from .base import Tracker


class MemoryTracker(Tracker):
    """Deterministic local adapter for development and conformance tests."""

    def __init__(self, issues: list[dict[str, Any]] | list[Issue] | None = None) -> None:
        self.issues: dict[str, Issue] = {}
        for item in issues or []:
            issue = item if isinstance(item, Issue) else Issue.model_validate(item)
            self.issues[issue.id] = issue

    async def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        if not states:
            return []
        wanted = {normalize_state(state) for state in states}
        return [
            issue.model_copy(deep=True)
            for issue in self.issues.values()
            if normalize_state(issue.state) in wanted
        ]

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return [
            self.issues[issue_id].model_copy(deep=True)
            for issue_id in issue_ids
            if issue_id in self.issues
        ]

    def update(self, issue_id: str, **changes: Any) -> None:
        current = self.issues[issue_id].model_dump()
        current.update(changes)
        self.issues[issue_id] = Issue.model_validate(current)
