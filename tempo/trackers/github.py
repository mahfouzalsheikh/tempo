from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from tempo.domain import Issue, normalize_state
from tempo.errors import TrackerError
from tempo.publication import prepare_publication, publication_error

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
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise TrackerError("Repository must be owner/name", category="tracker_config")
        self.repo = repo
        self.publication = None
        self._validation_fingerprint = None
        host = "github.com" if parsed.hostname == "api.github.com" else parsed.netloc
        self.git_remote_url = f"{parsed.scheme}://{host}/{repo}.git"
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
        tracker.publication = None
        tracker._validation_fingerprint = None
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
            error = TrackerError(
                f"GitHub returned {response.status_code}: {response.text[:500]}",
                category="tracker_response",
            )
            error.status_code = response.status_code
            raise error
        if method.upper() == "HEAD":
            return {"status": response.status_code}
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

    async def prepare_publication(self, workspace, run_id, state, save):
        self.publication = await prepare_publication(self, workspace, run_id, state, save)

    async def unchanged_from_base(self) -> bool:
        return bool(self.publication) and await self.publication.unchanged_from_base()

    def agent_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "github_api",
                "description": "Read issues, pull requests and source in this repository.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "HEAD"]},
                        "path": {"type": "string"},
                        "params": {"type": "object"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "github_publish",
                "description": (
                    "Publish the clean, validated local commit to Tempo's run branch and create "
                    "or recover its pull request. Commit changes before calling. Tempo chooses "
                    "the repository, branch and base; shell credentials are unnecessary."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "github_comment",
                "description": "Post an idempotent progress comment to this run's source issue.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"body": {"type": "string"}},
                    "required": ["body"],
                    "additionalProperties": False,
                },
            },
        ]

    def authorize_publication(self, issue_id: str) -> None:
        self._publication_authorized.add(issue_id)

    def revoke_publication(self, issue_id: str) -> None:
        self._publication_authorized.discard(issue_id)
        self._validation_fingerprint = None

    def accept_validation(self, fingerprint: str) -> None:
        self._validation_fingerprint = fingerprint

    @staticmethod
    def tool_result(success, output):
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return {
            "success": success,
            "output": text,
            "contentItems": [{"type": "inputText", "text": text}],
        }

    async def publish_candidate(self, arguments, issue, fingerprint):
        try:
            if issue.id not in self._publication_authorized or not self.publication:
                raise publication_error("Publication needs an active run and passing validation.")
            return self.tool_result(
                True,
                await self.publication.publish(
                    arguments,
                    issue,
                    self._validation_fingerprint or fingerprint,
                ),
            )
        except TrackerError as exc:
            return self.tool_result(
                False, {"error": {"message": str(exc), "category": exc.category}}
            )

    def verify_publication_event(self, event) -> bool:
        if (
            not self.publication
            or event.get("tool") != "github_publish"
            or not event.get("success")
        ):
            return False
        try:
            result = json.loads(event.get("output", ""))
        except (ValueError, TypeError):
            return False
        state = self.publication.state
        return result == {
            **state.get("pull_request", {}),
            "head_sha": state.get("published_sha"),
            "branch": state["branch"],
        }

    async def execute_agent_tool(self, name, arguments, issue):
        try:
            await self.assert_ownership()
            if name == "github_comment":
                body = arguments.get("body")
                if set(arguments) != {"body"} or not isinstance(body, str) or not body.strip():
                    raise publication_error("github_comment requires only a nonempty body.")
                if not str(issue.id).isdigit():
                    raise publication_error("A GitHub source issue is required.")
                marker = (
                    f"<!-- tempo-comment:{issue.id}:{hashlib.sha256(body.encode()).hexdigest()} -->"
                )
                await self._post_comment_once(
                    f"/repos/{self.repo}/issues/{issue.id}/comments",
                    marker,
                    f"{body}\n\n{marker}",
                )
                return self.tool_result(True, "Comment recorded.")
            if name != "github_api":
                raise publication_error(f"Unsupported tool: {name}")
            path = arguments.get("path")
            method = str(arguments.get("method", "GET")).upper()
            if method not in {"GET", "HEAD"}:
                raise publication_error(
                    "github_api is read-only. Use github_publish or github_comment. "
                    "Only Tempo's completed independent-review phase may merge a pull request."
                )
            # Refuse alternate spellings before the HTTP client can normalize them.
            prefix = f"/repos/{self.repo}"
            if (
                not isinstance(path, str)
                or (path != prefix and not path.startswith(prefix + "/"))
                or any(char in path for char in "%?#\\")
                or any(ord(char) < 33 for char in path)
                or any(part in {"", ".", ".."} for part in path[1:].split("/"))
                or set(arguments) - {"path", "method", "params"}
            ):
                raise publication_error("Use a canonical path inside the configured repository.")
            resource = path[len(prefix) + 1 :].split("/", 1)[0]
            if path != prefix and resource not in {
                "issues",
                "pulls",
                "contents",
                "commits",
                "compare",
                "branches",
                "tags",
                "git",
                "readme",
                "languages",
            }:
                raise publication_error("This repository resource is not exposed to agents.")
            output = await self._request(method, path, params=arguments.get("params"))
            return self.tool_result(True, output)
        except TrackerError as exc:
            return self.tool_result(
                False, {"error": {"message": str(exc), "category": exc.category}}
            )

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

        distinct_review_identity = bool(self.review_token and self.review_token != self.token)
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
                            "event": "APPROVE",
                            "body": review_body,
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
                        "GET",
                        f"/repos/{self.repo}/pulls/{pull_request_number}",
                    )
                except TrackerError:
                    reconciled = {}
                if (
                    isinstance(reconciled, dict)
                    and reconciled.get("merged")
                    and (reconciled.get("head") or {}).get("sha") == reviewed_head_sha
                ):
                    return await self._finalize_merged_review(
                        issue,
                        pull_request_number,
                        summary=summary,
                        merge=reconciled,
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
