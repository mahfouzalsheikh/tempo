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
        review_token: str | None = None,
        review_client: httpx.AsyncClient | None = None,
        token_env_name: str = "GITHUB_TOKEN",
        review_token_env_name: str = "GITHUB_REVIEW_TOKEN",
        required_labels: list[str] | None = None,
    ) -> None:
        parsed = urlparse(api_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise TrackerError("GitHub api_url must use HTTPS", category="tracker_config")
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.token = token or os.getenv(token_env_name) or None
        self.review_token = review_token or os.getenv(review_token_env_name) or None
        self.token_env_name = token_env_name
        self.review_token_env_name = review_token_env_name
        self.required_labels = {
            str(label).strip().lower() for label in (required_labels or []) if str(label).strip()
        }
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
        if review_client:
            self.review_client = review_client
            self._owns_review_client = False
        elif self.review_token:
            review_headers = dict(headers)
            review_headers["Authorization"] = f"Bearer {self.review_token}"
            self.review_client = httpx.AsyncClient(headers=review_headers, timeout=20)
            self._owns_review_client = True
        else:
            self.review_client = self.client
            self._owns_review_client = False

    async def close(self) -> None:
        if self._owns_review_client:
            await self.review_client.aclose()
        if self._owns_client:
            await self.client.aclose()

    def secret_environment_names(self) -> set[str]:
        return {
            "GITHUB_TOKEN",
            "GITHUB_REVIEW_TOKEN",
            self.token_env_name,
            self.review_token_env_name,
        }

    def for_run(self, ownership_check):
        tracker = super().for_run(ownership_check)
        tracker._publication_authorized = set()
        return tracker

    async def _request(
        self,
        method: str,
        path: str,
        *,
        review_identity: bool = False,
        **kwargs: Any,
    ) -> Any:
        if method.upper() not in {"GET", "HEAD"}:
            await self.assert_ownership()
        try:
            client = self.review_client if review_identity else self.client
            response = await client.request(method, f"{self.api_url}{path}", **kwargs)
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
                "output": (
                    "Only Tempo's completed independent-review phase may merge a pull request."
                ),
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
        issue_mutation = re.fullmatch(
            rf"/repos/{re.escape(self.repo)}/issues/\d+/?", path
        ) or re.fullmatch(rf"/repos/{re.escape(self.repo)}/issues/\d+/labels/?", path)
        if method not in {"GET", "HEAD"} and issue_mutation:
            return {
                "success": False,
                "output": (
                    "Tempo owns issue state and dispatch labels. Create the pull request or use "
                    "tempo_complete; direct issue mutations are not allowed."
                ),
                "contentItems": [],
            }
        body = arguments.get("body")
        if method == "POST" and path.rstrip("/") == f"/repos/{self.repo}/pulls":
            body = dict(body) if isinstance(body, dict) else {}
            closing_line = f"Closes #{issue.id}"
            description = str(body.get("body", "")).rstrip()
            if closing_line.lower() not in description.lower():
                body["body"] = f"{description}\n\n{closing_line}".strip()
            head = str(body.get("head", "")).strip()
            if head:
                qualified_head = head if ":" in head else f"{self.repo.split('/', 1)[0]}:{head}"
                existing = await self._request(
                    "GET",
                    f"/repos/{self.repo}/pulls",
                    params={"state": "open", "head": qualified_head},
                )
                if isinstance(existing, list) and existing:
                    text = json.dumps(existing[0], ensure_ascii=False)
                    return {
                        "success": True,
                        "output": text,
                        "contentItems": [{"type": "inputText", "text": text}],
                    }
        try:
            output = await self._request(
                method,
                path,
                params=arguments.get("params"),
                json=body,
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

    async def finalize_pull_request(self, issue: Issue, pull_request_number: int) -> None:
        await self.require_human_review(
            issue,
            pull_request_number,
            reason="Automated review is disabled by workflow policy.",
        )

    async def complete_pull_request_review(
        self,
        issue: Issue,
        pull_request_number: int,
        *,
        summary: str,
        reviewed_head_sha: str | None,
        auto_merge: bool,
        merge_method: str,
        reviewers: list[str],
        team_reviewers: list[str],
    ) -> dict[str, Any]:
        marker = f"<!-- tempo-review:{issue.id}:{pull_request_number}:{reviewed_head_sha} -->"
        review_body = (
            f"{marker}\nTempo's independent review agent approved commit "
            f"`{reviewed_head_sha}` for this pull request.\n\n{summary}"
        ).strip()

        try:
            current = await self._request(
                "GET",
                f"/repos/{self.repo}/pulls/{pull_request_number}",
            )
        except TrackerError:
            current = {}
        current_head = (current.get("head") or {}).get("sha") if isinstance(current, dict) else None
        if not reviewed_head_sha or current_head != reviewed_head_sha:
            return await self.require_human_review(
                issue,
                pull_request_number,
                reason=(
                    "Tempo could not confirm that the pull-request head matches the reviewed "
                    "commit. A fresh review of the published commit is required."
                ),
                summary=summary,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if isinstance(current, dict) and current.get("merged"):
            return await self._finalize_merged_review(
                issue,
                pull_request_number,
                summary=summary,
                merge=current,
            )

        distinct_review_identity = bool(
            self.review_token and self.review_token != self.token
        )
        if distinct_review_identity:
            try:
                reviews = await self._request(
                    "GET",
                    f"/repos/{self.repo}/pulls/{pull_request_number}/reviews",
                    review_identity=True,
                )
                already_approved = any(
                    isinstance(review, dict)
                    and marker in str(review.get("body", ""))
                    and str(review.get("state", "")).upper() == "APPROVED"
                    and review.get("commit_id") == reviewed_head_sha
                    for review in (reviews if isinstance(reviews, list) else [])
                )
                if not already_approved:
                    await self._request(
                        "POST",
                        f"/repos/{self.repo}/pulls/{pull_request_number}/reviews",
                        review_identity=True,
                        json={
                            "event": "APPROVE", "body": review_body,
                            "commit_id": reviewed_head_sha,
                        },
                    )
            except TrackerError as exc:
                return await self.require_human_review(
                    issue,
                    pull_request_number,
                    reason=f"GitHub rejected the configured reviewer identity: {exc}",
                    summary=summary,
                    reviewers=reviewers,
                    team_reviewers=team_reviewers,
                )
        else:
            # GitHub rejects APPROVE reviews authored by the pull-request author.
            # When the configured fallback is the publication token, preserve the
            # independent Tempo decision as a durable comment and let the merge API
            # enforce branch protection and required-review policy.
            await self._post_comment_once(
                f"/repos/{self.repo}/issues/{pull_request_number}/comments",
                marker,
                review_body,
            )

        if not auto_merge:
            return await self.require_human_review(
                issue,
                pull_request_number,
                reason="The review passed, but automatic merge is disabled by workflow policy.",
                summary=summary,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )

        try:
            merge = await self._request(
                "PUT",
                f"/repos/{self.repo}/pulls/{pull_request_number}/merge",
                json={"merge_method": merge_method, "sha": reviewed_head_sha},
            )
        except TrackerError as exc:
            # A transport error may hide a successful merge. Reconcile its identity
            # before handing off; never repeat a merge against an unreviewed head.
            if exc.category == "tracker_transport":
                try:
                    reconciled = await self._request(
                        "GET", f"/repos/{self.repo}/pulls/{pull_request_number}",
                    )
                except TrackerError:
                    reconciled = {}
                if (
                    isinstance(reconciled, dict)
                    and reconciled.get("merged")
                    and (reconciled.get("head") or {}).get("sha") == reviewed_head_sha
                ):
                    return await self._finalize_merged_review(
                        issue, pull_request_number, summary=summary, merge=reconciled,
                    )
            return await self.require_human_review(
                issue,
                pull_request_number,
                reason=(
                    "GitHub did not permit the automatic merge. Branch protection, required "
                    f"checks, permissions, or a merge conflict may require a person: {exc}"
                ),
                summary=summary,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )

        if not isinstance(merge, dict) or not merge.get("merged"):
            message = (
                str(merge.get("message", "")).strip()
                if isinstance(merge, dict)
                else "GitHub returned an unexpected merge response."
            )
            return await self.require_human_review(
                issue,
                pull_request_number,
                reason=(
                    "GitHub did not merge the reviewed pull request"
                    f"{f': {message}' if message else '.'}"
                ),
                summary=summary,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )

        return await self._finalize_merged_review(
            issue,
            pull_request_number,
            summary=summary,
            merge=merge,
        )

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
        reviewers = reviewers or []
        team_reviewers = team_reviewers or []
        if reviewers or team_reviewers:
            try:
                await self._request(
                    "POST",
                    f"/repos/{self.repo}/pulls/{pull_request_number}/requested_reviewers",
                    json={"reviewers": reviewers, "team_reviewers": team_reviewers},
                )
            except TrackerError:
                # The durable comments below remain the guaranteed handoff if reviewer assignment
                # is rejected because of repository membership or permissions.
                pass

        pull_request_url = f"https://github.com/{self.repo}/pull/{pull_request_number}"
        marker = f"<!-- tempo-human-review:{issue.id}:{pull_request_number} -->"
        body = (
            f"{marker}\n"
            "## Human review required\n\n"
            f"{reason.strip()}\n\n"
            f"{f'Tempo review summary: {summary.strip()}' if summary.strip() else ''}"
        ).strip()
        await self._post_comment_once(
            f"/repos/{self.repo}/issues/{pull_request_number}/comments",
            marker,
            body,
        )
        await self._post_comment_once(
            f"/repos/{self.repo}/issues/{issue.id}/comments",
            marker,
            (
                f"{marker}\nHuman review is required for "
                f"[pull request #{pull_request_number}]({pull_request_url}).\n\n{reason.strip()}"
            ),
        )
        await self._remove_dispatch_labels(issue)
        return {"status": "human_review", "reason": reason, "summary": summary}

    async def finalize_without_changes(self, issue: Issue, reason: str) -> None:
        comment_body = f"Tempo completed this ticket without a code change:\n\n{reason}"
        comments = await self._request(
            "GET",
            f"/repos/{self.repo}/issues/{issue.id}/comments",
            params={"per_page": 100},
        )
        if not any(
            isinstance(comment, dict) and comment.get("body") == comment_body
            for comment in (comments if isinstance(comments, list) else [])
        ):
            await self._request(
                "POST",
                f"/repos/{self.repo}/issues/{issue.id}/comments",
                json={"body": comment_body},
            )
        await self._remove_dispatch_labels(issue)

    async def _remove_dispatch_labels(self, issue: Issue) -> None:
        if not self.required_labels:
            return
        row = await self._request("GET", f"/repos/{self.repo}/issues/{issue.id}")
        labels = [
            str(item.get("name", ""))
            for item in row.get("labels", [])
            if isinstance(item, dict)
            and str(item.get("name", "")).strip().lower() not in self.required_labels
        ]
        await self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/{issue.id}",
            json={"labels": labels},
        )

    async def _post_comment_once(self, path: str, marker: str, body: str) -> None:
        comments = await self._request("GET", path, params={"per_page": 100})
        if any(
            isinstance(comment, dict) and marker in str(comment.get("body", ""))
            for comment in (comments if isinstance(comments, list) else [])
        ):
            return
        await self._request("POST", path, json={"body": body})

    async def _finalize_merged_review(
        self,
        issue: Issue,
        pull_request_number: int,
        *,
        summary: str,
        merge: dict[str, Any],
    ) -> dict[str, Any]:
        issue_marker = f"<!-- tempo-merge:{issue.id}:{pull_request_number} -->"
        pull_request_url = f"https://github.com/{self.repo}/pull/{pull_request_number}"
        await self._post_comment_once(
            f"/repos/{self.repo}/issues/{issue.id}/comments",
            issue_marker,
            (
                f"{issue_marker}\nTempo's independent review agent approved and merged "
                f"[pull request #{pull_request_number}]({pull_request_url}).\n\n{summary}"
            ).strip(),
        )
        await self._remove_dispatch_labels(issue)
        return {
            "status": "merged",
            "summary": summary,
            "sha": merge.get("sha") or merge.get("merge_commit_sha"),
            "message": merge.get("message"),
        }


def build_tracker(
    kind: str,
    provider: dict[str, Any],
    terminal_states: list[str],
    required_labels: list[str] | None = None,
) -> Tracker:
    if kind == "memory":
        return MemoryTracker(provider.get("issues") or [])
    token_value = provider.get("token")
    review_token_value = provider.get("review_token")
    token_env_name = "GITHUB_TOKEN"
    review_token_env_name = "GITHUB_REVIEW_TOKEN"
    if isinstance(token_value, str) and token_value.startswith("$"):
        token_env_name = token_value[1:]
        token_value = os.getenv(token_env_name)
    if isinstance(review_token_value, str) and review_token_value.startswith("$"):
        review_token_env_name = review_token_value[1:]
        review_token_value = os.getenv(review_token_env_name)
    return GitHubTracker(
        repo=str(provider["repo"]),
        token=token_value,
        review_token=review_token_value,
        api_url=str(provider.get("api_url", "https://api.github.com")),
        terminal_states=terminal_states,
        token_env_name=token_env_name,
        review_token_env_name=review_token_env_name,
        required_labels=required_labels,
    )
