from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from .codex import CodexAppServer
from .config import ServiceConfig
from .domain import Issue, RetryEntry, RunningEntry, Totals, normalize_state, utcnow
from .errors import CodexError
from .trackers.base import Tracker
from .trackers.github import build_tracker
from .workflow import WorkflowStore, render_prompt
from .workspace import WorkspaceManager

log = structlog.get_logger(__name__)
CONTINUATION_PROMPT = (
    "Continue working on the same issue. Check the tracker and current workspace state, "
    "then complete the next necessary steps. Do not repeat work already finished."
)
VALIDATION_POLICY_PROMPT = """

Tempo publication policy:
- Do not create a pull request until local validation succeeds.
- Discover how this repository is built, launched, and tested from its own documentation and files.
- Use the project_validation tool with the complete project-native validation sequence. Tempo, not
  your narrative assessment, determines success from the commands' exit codes.
- If validation fails, diagnose the output, fix the project, and run project_validation again.
- After validation passes, create a pull request but never merge it.
""".strip()
VALIDATION_CONTINUATION_PROMPT = (
    "Local validation has not passed yet. Inspect the repository's own instructions and tooling, "
    "then use project_validation to build or launch it and run the relevant tests. Fix failures "
    "and repeat. Do not create a pull request before validation passes."
)


class Orchestrator:
    """Single-authority in-memory Tempo scheduler."""

    def __init__(self, workflow_path: str) -> None:
        self.store = WorkflowStore(workflow_path)
        self.tracker: Tracker | None = None
        self.workspace: WorkspaceManager | None = None
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retries: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()
        self.totals = Totals()
        self.rate_limits: dict[str, Any] | None = None
        self.last_tick_at: datetime | None = None
        self.last_tick_error: str | None = None
        self.started_at = utcnow()
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._refresh = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._cancel_release: set[str] = set()
        self._retired_trackers: list[Tracker] = []

    async def start(self) -> None:
        _, config = await self.store.initialize()
        self._apply_config(config)
        await self._startup_cleanup()
        self._loop_task = asyncio.create_task(self._run_loop(), name="tempo-orchestrator")
        await log.ainfo(
            "orchestrator_started",
            workflow_path=str(self.store.path),
            tracker_kind=config.tracker.kind,
            workspace_root=str(config.workspace.root),
        )

    async def stop(self) -> None:
        self._stop.set()
        self._refresh.set()
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        tasks = [entry.task for entry in self.running.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.tracker:
            await self.tracker.close()
        for tracker in self._retired_trackers:
            await tracker.close()
        await log.ainfo("orchestrator_stopped")

    def _apply_config(self, config: ServiceConfig) -> None:
        if self.tracker is None:
            self.tracker = build_tracker(
                config.tracker.kind,
                config.tracker.provider,
                config.tracker.terminal_states,
            )
        self.workspace = WorkspaceManager(config.workspace.root, config.hooks)

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_tick_error = str(exc)
                await log.aexception("poll_tick_failed", error=str(exc))
            _, config = self.store.current()
            delay = config.polling.interval_ms / 1000
            if self.retries:
                earliest = min(entry.due_at for entry in self.retries.values())
                delay = min(delay, max((earliest - utcnow()).total_seconds(), 0.05))
            self._refresh.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._refresh.wait(), timeout=delay)

    async def tick(self) -> None:
        changed = await self.store.reload_if_changed()
        definition, config = self.store.current()
        if changed:
            assert self.tracker
            self._retired_trackers.append(self.tracker)
            self.tracker = build_tracker(
                config.tracker.kind,
                config.tracker.provider,
                config.tracker.terminal_states,
            )
            self.workspace = WorkspaceManager(config.workspace.root, config.hooks)
            await log.ainfo("workflow_reloaded", workflow_path=str(definition.path))
        self.last_tick_at = utcnow()
        self.last_tick_error = self.store.last_error
        await self._reconcile(config)
        await self._process_due_retries(config)
        assert self.tracker
        candidates = await self.tracker.fetch_issues_by_states(config.tracker.active_states)
        for issue in sorted(candidates, key=self._sort_key):
            async with self._lock:
                if not self._eligible(issue, config):
                    continue
                if not self._slot_available(issue, config):
                    break
                self._dispatch_locked(issue, None)

    async def refresh(self) -> None:
        self._refresh.set()

    async def _startup_cleanup(self) -> None:
        _, config = self.store.current()
        assert self.tracker and self.workspace
        try:
            terminal = await self.tracker.fetch_issues_by_states(config.tracker.terminal_states)
            for issue in terminal:
                await self.workspace.remove(issue.identifier)
        except Exception as exc:
            await log.awarning("startup_cleanup_failed", error=str(exc))

    async def _reconcile(self, config: ServiceConfig) -> None:
        assert self.tracker
        now = utcnow()
        for issue_id, entry in list(self.running.items()):
            last = entry.session.last_codex_timestamp or entry.started_at
            if (
                config.codex.stall_timeout_ms > 0
                and (now - last).total_seconds() * 1000 > config.codex.stall_timeout_ms
            ):
                await self._cancel_running(issue_id, release=False, cleanup=False)
                await log.awarning("agent_stalled", issue_id=issue_id)
        running_ids = list(self.running)
        if not running_ids:
            return
        try:
            refreshed = await self.tracker.fetch_issues_by_ids(running_ids)
        except Exception as exc:
            await log.awarning("reconciliation_failed", error=str(exc))
            return
        by_id = {issue.id: issue for issue in refreshed}
        for issue_id in running_ids:
            issue = by_id.get(issue_id)
            if issue is None:
                await self._cancel_running(issue_id, release=True, cleanup=False)
            elif normalize_state(issue.state) in config.terminal_states:
                await self._cancel_running(issue_id, release=True, cleanup=True)
            elif normalize_state(issue.state) not in config.active_states or not self._routable(
                issue, config
            ):
                await self._cancel_running(issue_id, release=True, cleanup=False)
            elif issue_id in self.running:
                self.running[issue_id].issue = issue

    async def _cancel_running(self, issue_id: str, *, release: bool, cleanup: bool) -> None:
        entry = self.running.get(issue_id)
        if not entry:
            return
        if release:
            self._cancel_release.add(issue_id)
        entry.task.cancel()
        if cleanup and self.workspace:
            await self.workspace.remove(entry.issue.identifier)

    async def _process_due_retries(self, config: ServiceConfig) -> None:
        assert self.tracker and self.workspace
        due = [entry for entry in self.retries.values() if entry.due_at <= utcnow()]
        for retry in due:
            self.retries.pop(retry.issue_id, None)
            try:
                issues = await self.tracker.fetch_issues_by_ids([retry.issue_id])
            except Exception as exc:
                self._schedule_retry(
                    retry.issue_id,
                    retry.identifier,
                    retry.attempt + 1,
                    str(exc),
                    config,
                )
                continue
            issue = next((item for item in issues if item.id == retry.issue_id), None)
            if issue is None:
                self.claimed.discard(retry.issue_id)
                continue
            if normalize_state(issue.state) in config.terminal_states:
                await self.workspace.remove(issue.identifier)
                self.claimed.discard(issue.id)
                continue
            if normalize_state(issue.state) not in config.active_states or not self._routable(
                issue, config
            ):
                self.claimed.discard(issue.id)
                continue
            async with self._lock:
                if self._slot_available(issue, config):
                    self._dispatch_locked(issue, retry.attempt)
                else:
                    self._schedule_retry(
                        issue.id,
                        issue.identifier,
                        retry.attempt + 1,
                        "no available orchestrator slots",
                        config,
                    )

    def _eligible(self, issue: Issue, config: ServiceConfig) -> bool:
        return (
            issue.id not in self.claimed
            and issue.id not in self.running
            and normalize_state(issue.state) in config.active_states
            and normalize_state(issue.state) not in config.terminal_states
            and self._routable(issue, config)
        )

    @staticmethod
    def _routable(issue: Issue, config: ServiceConfig) -> bool:
        required = set(config.tracker.required_labels)
        return issue.dispatchable and required.issubset(set(issue.labels)) and "" not in required

    def _slot_available(self, issue: Issue, config: ServiceConfig) -> bool:
        if len(self.running) >= config.agent.max_concurrent_agents:
            return False
        state = normalize_state(issue.state)
        limit = config.agent.max_concurrent_agents_by_state.get(
            state, config.agent.max_concurrent_agents
        )
        count = sum(normalize_state(entry.issue.state) == state for entry in self.running.values())
        return count < limit

    @staticmethod
    def _sort_key(issue: Issue) -> tuple[Any, ...]:
        priority = issue.priority if issue.priority in {1, 2, 3, 4} else 5
        created = issue.created_at or datetime.max.replace(tzinfo=UTC)
        return priority, created, issue.identifier

    def _dispatch_locked(self, issue: Issue, attempt: int | None) -> None:
        task = asyncio.create_task(
            self._run_worker(issue, attempt),
            name=f"tempo-{issue.identifier}",
        )
        self.running[issue.id] = RunningEntry(issue=issue, task=task, attempt=attempt)
        self.claimed.add(issue.id)
        self.retries.pop(issue.id, None)
        task.add_done_callback(
            lambda completed, issue_id=issue.id: asyncio.create_task(
                self._worker_finished(issue_id, completed)
            )
        )

    async def _run_worker(self, issue: Issue, attempt: int | None) -> None:
        definition, config = self.store.current()
        assert self.workspace and self.tracker
        workspace_manager = self.workspace
        tracker = self.tracker
        workspace = await workspace_manager.create(issue.identifier)

        async def on_event(event: dict[str, Any]) -> None:
            await self._codex_event(issue.id, event)

        client = CodexAppServer(config, workspace_manager, tracker, on_event)
        session = None
        try:
            tracker.revoke_publication(issue.id)
            if not config.validation.enabled:
                tracker.authorize_publication(issue.id)
            await workspace_manager.before_run(workspace.path)
            self.running[issue.id].phase = "LaunchingAgentProcess"
            session = await client.start_session(workspace.path)
            current_issue = issue
            for turn_number in range(1, config.agent.max_turns + 1):
                entry = self.running.get(issue.id)
                if entry:
                    entry.phase = "StreamingTurn"
                    entry.session.turn_count = turn_number
                prompt = (
                    (
                        f"{render_prompt(definition, current_issue, attempt)}\n\n"
                        f"{VALIDATION_POLICY_PROMPT}"
                        if config.validation.enabled
                        else render_prompt(definition, current_issue, attempt)
                    )
                    if turn_number == 1
                    else (
                        VALIDATION_CONTINUATION_PROMPT
                        if config.validation.enabled
                        and entry
                        and entry.session.validation_status != "passed"
                        else CONTINUATION_PROMPT
                    )
                )
                await client.run_turn(session, prompt, current_issue)
                if (
                    config.validation.enabled
                    and entry
                    and entry.session.validation_status != "passed"
                    and turn_number == config.agent.max_turns
                ):
                    raise CodexError(
                        "agent exhausted its turns without passing local project validation",
                        category="validation_required",
                    )
                if entry and entry.session.pull_request_created:
                    break
                refreshed = await tracker.fetch_issues_by_ids([issue.id])
                if not refreshed:
                    break
                current_issue = refreshed[0]
                if normalize_state(
                    current_issue.state
                ) not in config.active_states or not self._routable(current_issue, config):
                    break
        finally:
            if session:
                await client.stop_session(session)
            await workspace_manager.after_run(workspace.path)

    async def _codex_event(self, issue_id: str, event: dict[str, Any]) -> None:
        entry = self.running.get(issue_id)
        if not entry:
            return
        session = entry.session
        session.last_codex_event = str(event.get("event"))
        session.last_codex_timestamp = utcnow()
        session.last_codex_message = event.get("payload", event)
        session.recent_events.append(
            {
                "event": str(event.get("event", "unknown")),
                "at": utcnow().isoformat(),
                "tool": event.get("tool"),
                "success": event.get("success"),
            }
        )
        session.recent_events[:] = session.recent_events[-25:]
        session.session_id = event.get("session_id", session.session_id)
        session.thread_id = event.get("thread_id", session.thread_id)
        session.turn_id = event.get("turn_id", session.turn_id)
        session.codex_app_server_pid = event.get(
            "codex_app_server_pid", session.codex_app_server_pid
        )
        usage = event.get("usage") or {}
        session.codex_input_tokens = max(
            session.codex_input_tokens, int(usage.get("input_tokens", 0))
        )
        session.codex_output_tokens = max(
            session.codex_output_tokens, int(usage.get("output_tokens", 0))
        )
        session.codex_total_tokens = max(
            session.codex_total_tokens, int(usage.get("total_tokens", 0))
        )
        if "rate_limits" in event:
            self.rate_limits = event["rate_limits"]
        event_name = event.get("event")
        if event_name == "validation_started":
            entry.phase = "ValidatingProject"
            session.validation_status = "running"
            session.validation_summary = str(event.get("summary", ""))
            session.validation_started_at = utcnow()
            session.validation_finished_at = None
            session.validation_commands = []
        elif event_name == "validation_command_started":
            session.current_validation_command = str(event.get("command", ""))
        elif event_name == "validation_command_completed":
            session.current_validation_command = None
            session.validation_commands.append(
                {
                    "name": event.get("name"),
                    "command": event.get("command"),
                    "exit_code": event.get("exit_code"),
                    "output": event.get("output"),
                    "cleanup": bool(event.get("cleanup")),
                }
            )
        elif event_name == "validation_completed":
            session.validation_status = "passed" if event.get("success") else "failed"
            session.validation_finished_at = utcnow()
            if event.get("success"):
                self.totals.validation_passes += 1
            else:
                self.totals.validation_failures += 1
            entry.phase = "PreparingPullRequest" if event.get("success") else "FixingValidation"
        elif event_name == "validation_invalidated":
            session.validation_status = "pending"
            session.validation_finished_at = None
            entry.phase = "ValidationRequired"
        elif event_name == "tool_call_completed" and event.get("tool") == "github_api":
            arguments = event.get("arguments") or {}
            if str(arguments.get("method", "GET")).upper() == "POST" and str(
                arguments.get("path", "")
            ).rstrip().endswith("/pulls"):
                if event.get("success"):
                    entry.phase = "PullRequestCreated"
                    session.pull_request_created = True
                    with contextlib.suppress(json.JSONDecodeError):
                        payload = json.loads(str(event.get("output", "")))
                        session.pull_request_url = payload.get("html_url")

    async def _worker_finished(self, issue_id: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            entry = self.running.pop(issue_id, None)
            if not entry:
                return
            runtime = (utcnow() - entry.started_at).total_seconds()
            self.totals.runtime_seconds += runtime
            self.totals.input_tokens += entry.session.codex_input_tokens
            self.totals.output_tokens += entry.session.codex_output_tokens
            self.totals.total_tokens += entry.session.codex_total_tokens
            if issue_id in self._cancel_release:
                self._cancel_release.discard(issue_id)
                self.claimed.discard(issue_id)
                self.retries.pop(issue_id, None)
                return
            _, config = self.store.current()
            if task.cancelled():
                error = "worker cancelled or stalled"
                next_attempt = (entry.attempt or 0) + 1
                self._schedule_retry(issue_id, entry.issue.identifier, next_attempt, error, config)
                return
            error = task.exception()
            if error is None:
                self.completed.add(issue_id)
                if not entry.session.pull_request_created:
                    self._schedule_retry(
                        issue_id,
                        entry.issue.identifier,
                        1,
                        None,
                        config,
                        continuation=True,
                    )
            else:
                next_attempt = (entry.attempt or 0) + 1
                self._schedule_retry(
                    issue_id,
                    entry.issue.identifier,
                    next_attempt,
                    str(error),
                    config,
                )
                await log.aerror(
                    "worker_failed",
                    issue_id=issue_id,
                    issue_identifier=entry.issue.identifier,
                    error=str(error),
                )
        self._refresh.set()
        if not self.running and self._retired_trackers:
            retired, self._retired_trackers = self._retired_trackers, []
            for tracker in retired:
                await tracker.close()

    def _schedule_retry(
        self,
        issue_id: str,
        identifier: str,
        attempt: int,
        error: str | None,
        config: ServiceConfig,
        *,
        continuation: bool = False,
    ) -> None:
        delay_ms = (
            1000
            if continuation
            else min(
                10_000 * (2 ** min(max(attempt - 1, 0), 20)),
                config.agent.max_retry_backoff_ms,
            )
        )
        self.retries[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at=utcnow() + timedelta(milliseconds=delay_ms),
            error=error,
        )
        self.claimed.add(issue_id)

    def snapshot(self) -> dict[str, Any]:
        _, config = self.store.current()
        state_counts = Counter(
            normalize_state(entry.issue.state) for entry in self.running.values()
        )
        return {
            "service": {
                "started_at": self.started_at.isoformat(),
                "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
                "last_tick_error": self.last_tick_error,
                "workflow_path": str(self.store.path),
                "workflow_error": self.store.last_error,
                "tracker_kind": config.tracker.kind,
                "poll_interval_ms": config.polling.interval_ms,
                "max_concurrent_agents": config.agent.max_concurrent_agents,
                "workspace_root": str(config.workspace.root),
                "validation_enabled": config.validation.enabled,
                "validation_timeout_ms": config.validation.command_timeout_ms,
                "approval_policy": config.codex.approval_policy,
                "thread_sandbox": config.codex.thread_sandbox,
            },
            "running": [
                {
                    "issue_id": issue_id,
                    "identifier": entry.issue.identifier,
                    "title": entry.issue.title,
                    "state": entry.issue.state,
                    "url": entry.issue.url,
                    "attempt": entry.attempt,
                    "phase": entry.phase,
                    "started_at": entry.started_at.isoformat(),
                    "session": {
                        **entry.session.__dict__,
                        "last_codex_timestamp": (
                            entry.session.last_codex_timestamp.isoformat()
                            if entry.session.last_codex_timestamp
                            else None
                        ),
                        "validation_started_at": (
                            entry.session.validation_started_at.isoformat()
                            if entry.session.validation_started_at
                            else None
                        ),
                        "validation_finished_at": (
                            entry.session.validation_finished_at.isoformat()
                            if entry.session.validation_finished_at
                            else None
                        ),
                    },
                }
                for issue_id, entry in self.running.items()
            ],
            "retries": [
                {
                    "issue_id": entry.issue_id,
                    "identifier": entry.identifier,
                    "attempt": entry.attempt,
                    "due_at": entry.due_at.isoformat(),
                    "error": entry.error,
                }
                for entry in self.retries.values()
            ],
            "claimed_count": len(self.claimed),
            "completed_count": len(self.completed),
            "running_by_state": dict(state_counts),
            "totals": self.totals.__dict__,
            "rate_limits": self.rate_limits,
        }

    def admin_snapshot(self) -> dict[str, Any]:
        definition, config = self.store.current()
        return {
            "service": self.snapshot()["service"],
            "workflow": {
                "path": str(definition.path),
                "prompt_chars": len(definition.prompt_template),
                "last_reload_error": self.store.last_error,
            },
            "tracker": {
                "kind": config.tracker.kind,
                "repository": config.tracker.provider.get("repo"),
                "required_labels": config.tracker.required_labels,
                "active_states": config.tracker.active_states,
                "terminal_states": config.tracker.terminal_states,
            },
            "agents": {
                "max_concurrent": config.agent.max_concurrent_agents,
                "max_turns": config.agent.max_turns,
                "per_state": config.agent.max_concurrent_agents_by_state,
                "max_retry_backoff_ms": config.agent.max_retry_backoff_ms,
            },
            "validation": {
                "enabled": config.validation.enabled,
                "runner": (
                    "isolated"
                    if config.validation.runner_url or os.getenv("TEMPO_VALIDATION_RUNNER_URL")
                    else "local process"
                ),
                "command_timeout_ms": config.validation.command_timeout_ms,
                "cleanup_timeout_ms": config.validation.cleanup_timeout_ms,
                "max_commands": config.validation.max_commands,
                "max_output_chars": config.validation.max_output_chars,
                "publication_gate": "GitHub writes locked until validation passes",
                "merge_policy": "Tempo never merges branches or pull requests",
            },
            "hooks": {
                "after_create": bool(config.hooks.after_create),
                "before_run": bool(config.hooks.before_run),
                "after_run": bool(config.hooks.after_run),
                "before_remove": bool(config.hooks.before_remove),
                "timeout_ms": config.hooks.timeout_ms,
            },
            "runtime": {
                "running": len(self.running),
                "claimed": len(self.claimed),
                "queued_retries": len(self.retries),
                "completed": len(self.completed),
                "rate_limits": self.rate_limits,
            },
        }

    def issue_snapshot(self, identifier: str) -> dict[str, Any] | None:
        snapshot = self.snapshot()
        for category in ("running", "retries"):
            for row in snapshot[category]:
                if row["identifier"] == identifier:
                    return {"status": category.rstrip("s"), **row}
        return None
