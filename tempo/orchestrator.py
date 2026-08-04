from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from .activity import activity_from_event, append_activity
from .agent_runtime import providers
from .codex import CodexAppServer
from .config import ServiceConfig, WorkflowEdgeConfig, WorkflowNodeConfig
from .domain import (
    Issue,
    LiveSession,
    NodeExecutionState,
    RetryEntry,
    RunningEntry,
    Totals,
    normalize_state,
    utcnow,
)
from .errors import CodexError
from .persistence import PersistenceStore
from .trackers.base import Tracker
from .trackers.github import build_tracker
from .workflow import WorkflowStore, render_node_prompt, render_prompt
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
- After validation passes, create a pull request. The implementation agent must never merge it;
  Tempo starts an independent review agent and applies merge policy afterward.
- Validation commands must not edit project files.
- If the requested behavior is already present and validation passes, use tempo_complete with
  concrete evidence instead of repeating work or creating an empty pull request.
""".strip()
VALIDATION_CONTINUATION_PROMPT = (
    "Local validation has not passed yet. Inspect the repository's own instructions and tooling, "
    "then use project_validation to build or launch it and run the relevant tests. Fix failures "
    "and repeat. Do not create a pull request before validation passes."
)
REVIEW_POLICY_PROMPT = """

Tempo independent review policy:
- You are a separate reviewer, not a continuation of the implementation agent.
- Inspect the full issue and pull-request diff plus the repository's own instructions.
- Check correctness, regressions, security, tests, and maintainability.
- You may fix material findings and update the pull-request branch; validate the final workspace
  again after every change.
- Never call GitHub's merge API yourself. Tempo applies merge policy after your decision.
- Finish with tempo_review. Use approve only after unchanged local validation passes.
- Use human_review when ambiguity, sensitive risk, repository policy, permissions, or unresolved
  findings require a person, and state the exact reason.
""".strip()
REVIEW_CONTINUATION_PROMPT = (
    "Continue the independent pull-request review. Resolve remaining findings, validate the final "
    "workspace, then call tempo_review with approve or human_review and concrete evidence."
)
RECOVERY_CONTINUATION_PROMPT = (
    "Tempo resumed this durable thread after an operator unblock or worker recovery. "
    "Continue from the latest completed work and checkpoint. Inspect the current workspace and "
    "tracker state before acting, and do not repeat implementation, validation, commits, or "
    "publication steps that are already complete."
)
RECOVERY_FALLBACK_PROMPT = (
    "Tempo could not reopen the prior Codex thread, so recover from durable state rather than "
    "starting the issue over. Inspect the existing workspace, git history, tracker, open pull "
    "requests, and validation checkpoints first. Preserve completed work and perform only the "
    "remaining steps."
)


class Orchestrator:
    """Control plane backed by durable database claims and worker leases."""

    def __init__(self, workflow_path: str) -> None:
        self.store = WorkflowStore(workflow_path)
        self.tracker: Tracker | None = None
        self.workspace: WorkspaceManager | None = None
        self.persistence: PersistenceStore | None = None
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retries: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()
        self.safety_blocked: set[str] = set()
        self.historical_completed_count = 0
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
        self._event_subscribers: set[asyncio.Queue[None]] = set()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._operator_outcomes: dict[str, str] = {}
        self._feedback: dict[str, str] = {}
        self._workflow_config_updated_at: datetime | None = None

    async def start(self) -> None:
        _, config = await self.store.initialize()
        await self._apply_config(config)
        if self.persistence:
            managed_sections, updated_at = await self.persistence.workflow_configuration()
            if managed_sections:
                config = await self.store.replace_platform_sections(managed_sections)
                self.persistence.config = config
                await self.persistence.initialize()
                self._workflow_config_updated_at = updated_at
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

    async def _apply_config(self, config: ServiceConfig) -> None:
        if self.tracker is None:
            self.tracker = build_tracker(
                config.tracker.kind,
                config.tracker.provider,
                config.tracker.terminal_states,
                config.tracker.required_labels,
            )
        self.workspace = WorkspaceManager(config.workspace.root, config.hooks)
        self.persistence = PersistenceStore(
            config.tracker.kind,
            config=config,
            workflow_path=self.store.path,
        )
        await self.persistence.initialize()
        await self.persistence.reconcile_incomplete_records()
        self.totals, self.historical_completed_count = await self.persistence.runtime_summary()
        self.completed = await self.persistence.completed_issue_ids()
        self.safety_blocked = await self.persistence.safety_blocked_issue_ids()
        await self._restore_pending_runs()

    async def _restore_pending_runs(self) -> None:
        if not self.persistence:
            return
        for row in await self.persistence.pending_runs():
            if row["issue_id"] in self.running or row["issue_id"] in self.retries:
                continue
            self.retries[row["issue_id"]] = RetryEntry(
                issue_id=row["issue_id"],
                identifier=row["identifier"],
                attempt=row["attempt"] or 0,
                due_at=row["due_at"],
                error=row["error"],
                run_record_id=row["run_id"],
            )
            self.claimed.add(row["issue_id"])
            if row["feedback"]:
                self._feedback[row["issue_id"]] = row["feedback"]

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
        await self._reload_database_workflow_if_changed()
        changed = await self.store.reload_if_changed()
        definition, config = self.store.current()
        if changed:
            assert self.tracker
            self._retired_trackers.append(self.tracker)
            self.tracker = build_tracker(
                config.tracker.kind,
                config.tracker.provider,
                config.tracker.terminal_states,
                config.tracker.required_labels,
            )
            self.workspace = WorkspaceManager(config.workspace.root, config.hooks)
            self.persistence = PersistenceStore(
                config.tracker.kind,
                config=config,
                workflow_path=self.store.path,
            )
            await self.persistence.initialize()
            await self.persistence.reconcile_incomplete_records()
            (
                self.totals,
                self.historical_completed_count,
            ) = await self.persistence.runtime_summary()
            self.completed = await self.persistence.completed_issue_ids()
            self.safety_blocked = await self.persistence.safety_blocked_issue_ids()
            await self._restore_pending_runs()
            await log.ainfo("workflow_reloaded", workflow_path=str(definition.path))
        self.last_tick_at = utcnow()
        self.last_tick_error = self.store.last_error
        if self.persistence:
            await self.persistence.reconcile_incomplete_records()
            await self._restore_pending_runs()
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
                run_id = await self.persistence.enqueue_issue(issue) if self.persistence else None
                if run_id is None:
                    continue
                if self.persistence and not await self.persistence.claim_run(
                    run_id, self.worker_id
                ):
                    continue
                self._dispatch_locked(issue, None, run_record_id=run_id)

    async def _reload_database_workflow_if_changed(self) -> bool:
        if not self.persistence:
            return False
        sections, updated_at = await self.persistence.workflow_configuration()
        if not sections or updated_at == self._workflow_config_updated_at:
            return False
        config = await self.store.replace_platform_sections(sections)
        self.persistence.config = config
        await self.persistence.initialize()
        self._workflow_config_updated_at = updated_at
        self._publish_live_state()
        await log.ainfo(
            "database_workflow_reloaded",
            project_id=self.persistence.project_id,
            updated_at=updated_at,
        )
        return True

    async def refresh(self) -> None:
        self._refresh.set()

    def subscribe_events(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._event_subscribers.add(queue)
        return queue

    def unsubscribe_events(self, queue: asyncio.Queue[None]) -> None:
        self._event_subscribers.discard(queue)

    def _publish_live_state(self) -> None:
        for queue in tuple(self._event_subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

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
                await self._schedule_retry(
                    retry.issue_id,
                    retry.identifier,
                    retry.attempt + 1,
                    str(exc),
                    config,
                    run_record_id=retry.run_record_id,
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
                    if self.persistence and retry.run_record_id:
                        if not await self.persistence.claim_run(
                            retry.run_record_id,
                            self.worker_id,
                        ):
                            continue
                    self._dispatch_locked(
                        issue,
                        retry.attempt,
                        run_record_id=retry.run_record_id,
                    )
                else:
                    await self._schedule_retry(
                        issue.id,
                        issue.identifier,
                        retry.attempt + 1,
                        "no available orchestrator slots",
                        config,
                        run_record_id=retry.run_record_id,
                    )

    def _eligible(self, issue: Issue, config: ServiceConfig) -> bool:
        return (
            issue.id not in self.claimed
            and issue.id not in self.running
            and issue.id not in self.completed
            and issue.id not in self.safety_blocked
            and normalize_state(issue.state) in config.active_states
            and normalize_state(issue.state) not in config.terminal_states
            and self._routable(issue, config)
        )

    @staticmethod
    def _routable(issue: Issue, config: ServiceConfig) -> bool:
        required = set(config.tracker.required_labels)
        return issue.dispatchable and required.issubset(set(issue.labels)) and "" not in required

    def _slot_available(self, issue: Issue, config: ServiceConfig) -> bool:
        global_limit = min(
            config.agent.max_concurrent_agents,
            config.project.max_concurrent_runs,
            config.project.environment_max_concurrent_runs,
        )
        if len(self.running) >= global_limit:
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

    def _dispatch_locked(
        self,
        issue: Issue,
        attempt: int | None,
        *,
        run_record_id: int | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._run_worker(issue, attempt),
            name=f"tempo-{issue.identifier}",
        )
        self.running[issue.id] = RunningEntry(
            issue=issue,
            task=task,
            attempt=attempt,
            run_record_id=run_record_id,
        )
        self.claimed.add(issue.id)
        self.retries.pop(issue.id, None)
        task.add_done_callback(
            lambda completed, issue_id=issue.id: asyncio.create_task(
                self._worker_finished(issue_id, completed)
            )
        )
        self._publish_live_state()

    async def _run_worker(self, issue: Issue, attempt: int | None) -> None:
        definition, config = self.store.current()
        assert self.workspace and self.tracker
        workspace_manager = self.workspace
        tracker = self.tracker
        workspace = await workspace_manager.create(issue.identifier)
        entry = self.running[issue.id]
        if self.persistence:
            entry.run_record_id = await self.persistence.start_run(entry, workspace.path)

        async def on_approval(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            return await self._wait_for_approval(issue.id, kind, payload)

        heartbeat_task = asyncio.create_task(
            self._heartbeat_worker(entry),
            name=f"tempo-heartbeat-{issue.identifier}",
        )

        try:
            tracker.revoke_publication(issue.id)
            if not config.validation.enabled:
                tracker.authorize_publication(issue.id)
            await workspace_manager.before_run(workspace.path)
            await self._execute_workflow_graph(
                issue,
                attempt,
                definition,
                config,
                workspace.path,
                workspace_manager,
                tracker,
            )
            entry = self.running.get(issue.id)
            if not entry:
                raise CodexError("run was released", category="run_released")
            self._aggregate_node_sessions(entry)
            if not config.workflow.require_publication:
                entry.phase = "WorkflowCompleted"
            elif entry.session.pull_request_created:
                if entry.session.pull_request_number is None:
                    raise CodexError(
                        "pull request response did not include a number",
                        category="publication_response",
                    )
                if config.review.enabled:
                    await self._run_review_agent(
                        issue,
                        workspace.path,
                        config,
                        workspace_manager,
                        tracker,
                        entry.session.pull_request_number,
                        on_approval,
                    )
                else:
                    entry.phase = "HumanReviewRequired"
                    entry.session.review_status = "human_review"
                    entry.session.human_review_reason = (
                        "Automated review is disabled by workflow policy."
                    )
                    await tracker.finalize_pull_request(
                        issue,
                        entry.session.pull_request_number,
                    )
            elif entry.session.no_change_completed:
                entry.phase = "NoChangesRequired"
                await tracker.finalize_without_changes(
                    issue,
                    entry.session.completion_summary or "No code change was required.",
                )
            else:
                raise CodexError(
                    "agent ended without creating a pull request or recording no-change completion",
                    category="completion_required",
                )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            await workspace_manager.after_run(workspace.path)

    async def _execute_workflow_graph(
        self,
        issue: Issue,
        attempt: int | None,
        definition: Any,
        config: ServiceConfig,
        workspace_path: Any,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
    ) -> None:
        entry = self.running[issue.id]
        for node in config.workflow.nodes:
            profile = config.agents.get(node.agent or "")
            model = config.model_providers.get(profile.model) if profile else None
            entry.graph_nodes[node.id] = NodeExecutionState(
                node_id=node.id,
                name=node.name or node.id.replace("-", " ").replace("_", " ").title(),
                node_type=node.type,
                agent=node.agent,
                role=profile.role if profile else None,
                runtime=profile.runtime if profile else None,
                model=(model.model or profile.model) if model and profile else None,
            )
        if self.persistence:
            await self.persistence.initialize_run_nodes(entry)
        for state in entry.graph_nodes.values():
            if state.status in {"running", "waiting", "failed", "cancelled"}:
                state.status = "pending"
                state.attempt = 0

        incoming: dict[str, list[WorkflowEdgeConfig]] = {
            node.id: [] for node in config.workflow.nodes
        }
        outgoing: dict[str, list[WorkflowEdgeConfig]] = {
            node.id: [] for node in config.workflow.nodes
        }
        by_id = {node.id: node for node in config.workflow.nodes}
        for edge in config.workflow.edges:
            incoming[edge.target].append(edge)
            outgoing[edge.source].append(edge)

        while True:
            pending = [state for state in entry.graph_nodes.values() if state.status == "pending"]
            if not pending:
                break
            ready: list[WorkflowNodeConfig] = []
            progressed = False
            for state in pending:
                dependencies = incoming[state.node_id]
                if not dependencies:
                    ready.append(by_id[state.node_id])
                    continue
                source_states = [entry.graph_nodes[edge.source] for edge in dependencies]
                if any(
                    source.status in {"pending", "running", "waiting"}
                    for source in source_states
                ):
                    continue
                matches = [
                    self._edge_matches(edge, entry.graph_nodes[edge.source], issue)
                    for edge in dependencies
                ]
                join_policy = str(by_id[state.node_id].settings.get("join", "all")).lower()
                should_run = any(matches) if join_policy == "any" else all(matches)
                if should_run:
                    ready.append(by_id[state.node_id])
                else:
                    state.status = "skipped"
                    state.finished_at = utcnow()
                    if self.persistence and entry.run_record_id:
                        await self.persistence.finish_run_node(
                            entry.run_record_id,
                            state.node_id,
                            status="skipped",
                            output={"reason": "dependency conditions did not match"},
                        )
                    progressed = True
            if not ready:
                if progressed:
                    continue
                raise CodexError(
                    "workflow graph cannot make progress", category="workflow_graph_deadlock"
                )
            for start in range(0, len(ready), config.workflow.max_parallel_nodes):
                batch = ready[start : start + config.workflow.max_parallel_nodes]
                tasks = [
                    asyncio.create_task(
                        self._execute_workflow_node(
                            issue,
                            attempt,
                            definition,
                            config,
                            node,
                            workspace_path,
                            workspace_manager,
                            tracker,
                        ),
                        name=f"tempo-{issue.identifier}-{node.id}",
                    )
                    for node in batch
                ]
                done, pending_tasks = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                error = next(
                    (
                        task.exception()
                        for task in done
                        if not task.cancelled() and task.exception() is not None
                    ),
                    None,
                )
                if error:
                    for task in pending_tasks:
                        task.cancel()
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                    raise error
                await asyncio.gather(*pending_tasks)

        unhandled = []
        for state in entry.graph_nodes.values():
            if state.status != "failed":
                continue
            matching_handlers = [
                edge
                for edge in outgoing[state.node_id]
                if self._edge_matches(edge, state, issue)
                and entry.graph_nodes[edge.target].status == "succeeded"
            ]
            if not matching_handlers:
                unhandled.append(state)
        if unhandled:
            failed = unhandled[0]
            raise CodexError(
                f"workflow node {failed.node_id} failed: {failed.error or 'unknown error'}",
                category="workflow_node_failed",
            )

    @staticmethod
    def _edge_matches(
        edge: WorkflowEdgeConfig,
        source: NodeExecutionState,
        issue: Issue,
    ) -> bool:
        condition = edge.condition.strip().lower()
        if condition in {"always", "completed"}:
            return source.status in {"succeeded", "failed", "skipped"}
        if condition in {"succeeded", "failed", "skipped"}:
            return source.status == condition
        if condition.startswith("issue.label:"):
            return condition.split(":", 1)[1].strip() in issue.labels
        if condition.startswith("not issue.label:"):
            return condition.split(":", 1)[1].strip() not in issue.labels
        return False

    async def _execute_workflow_node(
        self,
        issue: Issue,
        attempt: int | None,
        definition: Any,
        config: ServiceConfig,
        node: WorkflowNodeConfig,
        workspace_path: Any,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
    ) -> None:
        entry = self.running[issue.id]
        state = entry.graph_nodes[node.id]
        profile = config.agents.get(node.agent or "")
        model_candidates = providers.model_candidates(config, profile) if profile else ()
        maximum_attempts = max(node.max_retries + 1, len(model_candidates), 1)
        for node_attempt in range(state.attempt + 1, maximum_attempts + 1):
            state.status = "running"
            state.attempt = node_attempt
            state.started_at = utcnow()
            state.error = None
            entry.phase = f"Node:{node.id}"
            if self.persistence and entry.run_record_id:
                await self.persistence.start_run_node(entry.run_record_id, node.id, node_attempt)
            self._publish_live_state()
            try:
                if node.type == "join":
                    output = {"joined": True}
                elif node.type == "human_gate":
                    state.status = "waiting"
                    if self.persistence and entry.run_record_id:
                        from tempo_web.models import RunNode

                        await RunNode.objects.filter(
                            run_id=entry.run_record_id, node_key=node.id
                        ).aupdate(status=RunNode.Status.WAITING)
                    decision = await self._wait_for_approval(
                        issue.id,
                        f"workflow_gate:{node.id}",
                        {
                            "params": {
                                "reason": node.approval_message,
                                "node": node.id,
                            }
                        },
                    )
                    if not decision.get("approved"):
                        raise CodexError(
                            decision.get("note") or "workflow gate rejected",
                            category="workflow_gate_rejected",
                        )
                    output = {"approved": True, "note": decision.get("note", "")}
                else:
                    output = await self._execute_agent_node(
                        issue,
                        attempt,
                        definition,
                        config,
                        node,
                        workspace_path,
                        workspace_manager,
                        tracker,
                    )
                state.status = "succeeded"
                state.output = output
                state.finished_at = utcnow()
                if self.persistence and entry.run_record_id:
                    await self.persistence.finish_run_node(
                        entry.run_record_id,
                        node.id,
                        status="succeeded",
                        output=output,
                    )
                self._publish_live_state()
                return
            except asyncio.CancelledError:
                state.status = "cancelled"
                state.finished_at = utcnow()
                if self.persistence and entry.run_record_id:
                    await self.persistence.finish_run_node(
                        entry.run_record_id,
                        node.id,
                        status="cancelled",
                        error="node cancelled",
                    )
                raise
            except Exception as exc:
                state.error = str(exc)
                state.finished_at = utcnow()
                safety_limit = getattr(exc, "category", "") in {
                    "token_budget_exceeded",
                    "validation_attempt_limit",
                }
                if safety_limit:
                    state.status = "failed"
                    if self.persistence and entry.run_record_id:
                        await self.persistence.finish_run_node(
                            entry.run_record_id,
                            node.id,
                            status="failed",
                            error=str(exc),
                        )
                    raise
                if node_attempt < maximum_attempts:
                    state.status = "pending"
                    continue
                state.status = "failed"
                if self.persistence and entry.run_record_id:
                    await self.persistence.finish_run_node(
                        entry.run_record_id,
                        node.id,
                        status="failed",
                        error=str(exc),
                    )
                self._publish_live_state()
                return

    async def _execute_agent_node(
        self,
        issue: Issue,
        attempt: int | None,
        definition: Any,
        config: ServiceConfig,
        node: WorkflowNodeConfig,
        workspace_path: Any,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
    ) -> dict[str, Any]:
        entry = self.running[issue.id]
        profile = config.agents[node.agent or ""]
        live = LiveSession(active_node_id=node.id, active_agent_role=profile.role)
        entry.node_sessions[node.id] = live
        entry.session = live

        async def on_event(event: dict[str, Any]) -> None:
            event = {**event, "node_id": node.id, "agent_role": profile.role}
            await self._codex_event(issue.id, event, live_session=live)

        async def on_approval(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            return await self._wait_for_approval(issue.id, kind, payload)

        runtime = providers.create_runtime(
            config,
            profile,
            workspace_manager,
            tracker,
            on_event,
            on_approval,
            model_index=max(entry.graph_nodes[node.id].attempt - 1, 0),
        )
        selected_model = getattr(runtime, "selected_model", None)
        if selected_model:
            entry.graph_nodes[node.id].model = selected_model
            if self.persistence and entry.run_record_id:
                await self.persistence.set_run_node_model(
                    entry.run_record_id,
                    node.id,
                    selected_model,
                )
        entry.phase = f"Launching:{node.id}"
        self._publish_live_state()
        runtime_session = await runtime.start_session(workspace_path)
        current_issue = issue
        max_turns = profile.max_turns or config.agent.max_turns
        try:
            for turn_number in range(1, max_turns + 1):
                live.turn_count = turn_number
                entry.phase = f"Running:{node.id}"
                operator_feedback = self._feedback.pop(issue.id, None)
                if turn_number == 1:
                    graph_context = {
                        key: {
                            "status": value.status,
                            "output": value.output,
                        }
                        for key, value in entry.graph_nodes.items()
                        if key != node.id and value.status != "pending"
                    }
                    current_state = entry.graph_nodes.get(node.id)
                    node_context = {
                        "id": node.id,
                        "name": current_state.name if current_state else node.id,
                        "type": node.type,
                        "agent": node.agent,
                        "role": profile.role,
                    }
                    prompt_parts = [render_prompt(definition, current_issue, attempt)]
                    for template in (profile.prompt, node.prompt):
                        if template.strip():
                            prompt_parts.append(
                                render_node_prompt(
                                    template,
                                    issue=current_issue,
                                    attempt=attempt,
                                    node=node_context,
                                    graph=graph_context,
                                )
                            )
                    prompt_parts.append(
                        f"You are the {profile.role} specialist for node {node.id}."
                    )
                    if graph_context:
                        prompt_parts.append(
                            "Prior workflow results:\n"
                            + json.dumps(graph_context, sort_keys=True, default=str)
                        )
                    if (
                        profile.completion in {"validation", "publication"}
                        and config.validation.enabled
                    ):
                        prompt_parts.append(VALIDATION_POLICY_PROMPT)
                    prompt = "\n\n".join(part for part in prompt_parts if part)
                else:
                    prompt = (
                        VALIDATION_CONTINUATION_PROMPT
                        if profile.completion in {"validation", "publication"}
                        and config.validation.enabled
                        and live.validation_status != "passed"
                        else CONTINUATION_PROMPT
                    )
                if operator_feedback:
                    prompt = f"Operator feedback:\n{operator_feedback}\n\n{prompt}"
                await runtime.run_turn(runtime_session, prompt, current_issue)
                if operator_feedback and self.persistence and entry.run_record_id:
                    await self.persistence.set_control_state(entry.run_record_id, feedback="")
                if profile.completion == "turn":
                    break
                if profile.completion == "validation" and live.validation_status == "passed":
                    break
                if profile.completion == "publication" and (
                    live.pull_request_created or live.no_change_completed
                ):
                    break
                refreshed = await tracker.fetch_issues_by_ids([issue.id])
                if refreshed:
                    current_issue = refreshed[0]
                    if self.persistence:
                        await self.persistence.sync_issue(current_issue)
            else:
                raise CodexError(
                    f"agent {node.agent} exhausted {max_turns} turns before {profile.completion}",
                    category=f"{profile.completion}_required",
                )
            summary = next(
                (
                    str(event.get("text", ""))
                    for event in reversed(live.recent_events)
                    if event.get("kind") == "message" and event.get("text")
                ),
                "",
            )
            return {
                "session_id": live.session_id,
                "thread_id": live.thread_id,
                "turns": live.turn_count,
                "input_tokens": live.codex_input_tokens,
                "output_tokens": live.codex_output_tokens,
                "total_tokens": live.codex_total_tokens,
                "validation_status": live.validation_status,
                "pull_request_url": live.pull_request_url,
                "no_change_completed": live.no_change_completed,
                "summary": summary[:4000],
            }
        finally:
            await runtime.stop_session(runtime_session)

    @staticmethod
    def _aggregate_node_sessions(entry: RunningEntry) -> None:
        if entry.node_sessions_aggregated:
            return
        sessions = list(entry.node_sessions.values())
        if not sessions:
            return
        publication = next(
            (
                session
                for session in reversed(sessions)
                if session.pull_request_created or session.no_change_completed
            ),
            sessions[-1],
        )
        publication.codex_input_tokens = sum(session.codex_input_tokens for session in sessions)
        publication.codex_output_tokens = sum(session.codex_output_tokens for session in sessions)
        publication.codex_total_tokens = sum(session.codex_total_tokens for session in sessions)
        entry.session = publication
        entry.node_sessions_aggregated = True

    async def _run_review_agent(
        self,
        issue: Issue,
        workspace_path: Any,
        config: ServiceConfig,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
        pull_request_number: int,
        approval_callback: Any,
        *,
        resume_context: dict[str, Any] | None = None,
    ) -> None:
        entry = self.running.get(issue.id)
        if not entry:
            raise CodexError("run was released before review", category="run_released")
        entry.phase = "LaunchingReviewAgent"
        entry.session.agent_role = "review"
        entry.session.review_status = "in_progress"
        if not resume_context:
            entry.session.thread_input_tokens = 0
            entry.session.thread_output_tokens = 0
            entry.session.thread_total_tokens = 0
        self._publish_live_state()

        base_input = entry.session.codex_input_tokens
        base_output = entry.session.codex_output_tokens
        base_total = entry.session.codex_total_tokens
        implementation_turns = entry.session.turn_count

        async def on_review_event(event: dict[str, Any]) -> None:
            usage = event.get("usage")
            if isinstance(usage, dict):
                event = {
                    **event,
                    "thread_usage": event.get("thread_usage", usage),
                    "usage": {
                        **usage,
                        "input_tokens": base_input + int(usage.get("input_tokens", 0)),
                        "output_tokens": base_output + int(usage.get("output_tokens", 0)),
                        "total_tokens": base_total + int(usage.get("total_tokens", 0)),
                    },
                }
            await self._codex_event(issue.id, event)

        review_client = CodexAppServer(
            config,
            workspace_manager,
            tracker,
            on_review_event,
            approval_callback=approval_callback,
        )
        review_session = None
        try:
            review_session_kwargs: dict[str, Any] = {"role": "review"}
            if resume_context:
                review_session_kwargs.update(
                    {
                        "resume_thread_id": str(resume_context["thread_id"]),
                        "usage_baseline": resume_context.get("usage_baseline"),
                    }
                )
            review_session = await review_client.start_session(
                workspace_path,
                **review_session_kwargs,
            )
            pull_request_url = (
                entry.session.pull_request_url or f"pull request #{pull_request_number}"
            )
            for review_turn in range(1, config.review.max_turns + 1):
                entry = self.running.get(issue.id)
                if not entry:
                    raise CodexError("run was released during review", category="run_released")
                entry.phase = "ReviewingPullRequest"
                entry.session.turn_count = implementation_turns + review_turn
                if review_turn == 1 and resume_context:
                    prompt = (
                        REVIEW_CONTINUATION_PROMPT
                        if review_session.resumed
                        else (
                            f"{RECOVERY_FALLBACK_PROMPT}\n\n"
                            f"Recover the independent review of {pull_request_url}.\n\n"
                            f"{config.review.prompt}\n\n{REVIEW_POLICY_PROMPT}"
                        )
                    )
                else:
                    prompt = (
                        (
                            f"Review {pull_request_url} for {issue.identifier}: {issue.title}.\n\n"
                            f"{config.review.prompt}\n\n{REVIEW_POLICY_PROMPT}"
                        )
                        if review_turn == 1
                        else REVIEW_CONTINUATION_PROMPT
                    )
                self._publish_live_state()
                await review_client.run_turn(review_session, prompt, issue)
                if review_session.review_decision:
                    break
            if not review_session.review_decision:
                raise CodexError(
                    "review agent ended without an approve or human-review decision",
                    category="review_completion_required",
                )

            summary = review_session.review_summary or "Independent review completed."
            if review_session.review_decision == "approve":
                entry.phase = "ApplyingMergePolicy"
                outcome = await tracker.complete_pull_request_review(
                    issue,
                    pull_request_number,
                    summary=summary,
                    auto_merge=config.review.auto_merge,
                    merge_method=config.review.merge_method,
                    reviewers=config.review.reviewers,
                    team_reviewers=config.review.team_reviewers,
                )
            else:
                outcome = await tracker.require_human_review(
                    issue,
                    pull_request_number,
                    reason=summary,
                    summary=summary,
                    reviewers=config.review.reviewers,
                    team_reviewers=config.review.team_reviewers,
                )

            status = str(outcome.get("status", "human_review"))
            entry.session.review_summary = summary
            entry.session.review_status = status
            if status == "merged":
                entry.phase = "Merged"
                entry.session.merged = True
                entry.session.human_review_reason = None
            else:
                reason = str(outcome.get("reason", summary))
                entry.phase = "HumanReviewRequired"
                entry.session.human_review_reason = reason
            await self._codex_event(
                issue.id,
                {
                    "event": "review_outcome",
                    "status": status,
                    "summary": summary,
                    "reason": entry.session.human_review_reason,
                },
            )
        finally:
            if review_session:
                await review_client.stop_session(review_session)

    async def _heartbeat_worker(self, entry: RunningEntry) -> None:
        while entry.issue.id in self.running:
            if self.persistence and entry.run_record_id:
                await self.persistence.heartbeat(entry.run_record_id)
            await asyncio.sleep(10)

    async def _wait_for_approval(
        self,
        issue_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        entry = self.running.get(issue_id)
        if not entry or not entry.run_record_id or not self.persistence:
            return {"approved": False, "note": "Run is no longer active."}
        request_key = hashlib.sha256(
            f"{entry.run_record_id}:{kind}:".encode()
            + json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        approval_id = await self.persistence.create_approval(
            entry.run_record_id,
            request_key=request_key,
            kind=kind,
            details=payload,
        )
        entry.phase = "WaitingForApproval"
        self._publish_live_state()
        while issue_id in self.running:
            decision = await self.persistence.approval_decision(approval_id)
            if decision is not None:
                await self.persistence.resume_after_approval(entry.run_record_id)
                entry.phase = "StreamingTurn"
                self._publish_live_state()
                return decision
            await asyncio.sleep(0.5)
        return {"approved": False, "note": "Run ended before approval was decided."}

    async def _codex_event(
        self,
        issue_id: str,
        event: dict[str, Any],
        *,
        live_session: LiveSession | None = None,
    ) -> None:
        entry = self.running.get(issue_id)
        if not entry:
            return
        session = live_session or entry.session
        entry.session = session
        session.last_codex_event = str(event.get("event"))
        session.last_codex_timestamp = utcnow()
        session.last_codex_message = event.get("payload", event)
        event_timestamp = utcnow()
        append_activity(
            session.recent_events,
            activity_from_event(event, event_timestamp.isoformat()),
        )
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
        _, config = self.store.current()
        if event_name == "validation_started":
            total_validation_attempts = sum(
                item.validation_attempt_count for item in entry.node_sessions.values()
            )
            if not entry.node_sessions:
                total_validation_attempts = session.validation_attempt_count
            if total_validation_attempts >= config.validation.max_attempts_per_run:
                entry.phase = "SafetyLimitReached"
                self._publish_live_state()
                raise CodexError(
                    (
                        "maximum validation attempts reached "
                        f"({config.validation.max_attempts_per_run})"
                    ),
                    category="validation_attempt_limit",
                )
            session.validation_attempt_count += 1
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
                if session.successful_validation_count == 0:
                    self.totals.validated_runs += 1
                session.successful_validation_count += 1
            else:
                self.totals.validation_failures += 1
            entry.phase = "PreparingPullRequest" if event.get("success") else "FixingValidation"
        elif event_name == "validation_invalidated":
            if session.validation_status == "passed":
                self.totals.validation_passes = max(0, self.totals.validation_passes - 1)
                session.successful_validation_count = max(
                    0, session.successful_validation_count - 1
                )
                if session.successful_validation_count == 0:
                    self.totals.validated_runs = max(0, self.totals.validated_runs - 1)
            elif session.validation_status == "failed":
                self.totals.validation_failures = max(0, self.totals.validation_failures - 1)
            session.validation_status = "pending"
            session.validation_finished_at = None
            entry.phase = "ValidationRequired"
        elif event_name == "tool_call_completed" and event.get("tool") == "github_api":
            pull_request = PersistenceStore._pull_request_from_checkpoint(event)
            if pull_request:
                entry.phase = "PullRequestCreated"
                session.pull_request_created = True
                session.pull_request_url, session.pull_request_number = pull_request
        elif event_name == "no_change_completed":
            entry.phase = "NoChangesRequired"
            session.no_change_completed = True
            session.completion_summary = str(event.get("reason", "")).strip()
        elif event_name == "review_completed":
            decision = str(event.get("decision", "human_review"))
            entry.phase = "ReviewApproved" if decision == "approve" else "HumanReviewRequired"
            session.review_status = decision
            session.review_summary = str(event.get("summary", "")).strip()
            if decision == "human_review":
                session.human_review_reason = session.review_summary
        self._publish_live_state()
        if self.persistence and "delta" not in str(event_name).lower():
            await self.persistence.record_event(entry, event, live_session=session)
            if entry.run_record_id:
                await self.persistence.heartbeat(entry.run_record_id)
                if session.active_node_id:
                    await self.persistence.record_run_node_event(
                        entry.run_record_id,
                        session.active_node_id,
                        session,
                    )
                if event_name in {
                    "validation_completed",
                    "validation_fingerprint_recorded",
                    "tool_call_completed",
                    "no_change_completed",
                }:
                    event_key = hashlib.sha256(
                        json.dumps(event, sort_keys=True, default=str).encode()
                    ).hexdigest()
                    await self.persistence.checkpoint(
                        entry.run_record_id,
                        str(event_name),
                        event,
                        idempotency_key=f"{entry.run_record_id}:{event_key}",
                    )
        total_tokens = sum(item.codex_total_tokens for item in entry.node_sessions.values())
        if not entry.node_sessions:
            total_tokens = session.codex_total_tokens
        if total_tokens > config.agent.max_tokens_per_run:
            entry.phase = "SafetyLimitReached"
            self._publish_live_state()
            raise CodexError(
                (
                    f"run exceeded token limit ({total_tokens} > "
                    f"{config.agent.max_tokens_per_run})"
                ),
                category="token_budget_exceeded",
            )

    async def _worker_finished(self, issue_id: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            entry = self.running.pop(issue_id, None)
            if not entry:
                return
            self._aggregate_node_sessions(entry)
            runtime = (utcnow() - entry.started_at).total_seconds()
            self.totals.runtime_seconds += runtime
            self.totals.input_tokens += entry.session.codex_input_tokens
            self.totals.output_tokens += entry.session.codex_output_tokens
            self.totals.total_tokens += entry.session.codex_total_tokens
            if issue_id in self._cancel_release:
                if self.persistence:
                    await self.persistence.finish_run(
                        entry,
                        status="cancelled",
                        error="run released after tracker state changed",
                    )
                self._cancel_release.discard(issue_id)
                self.claimed.discard(issue_id)
                self.retries.pop(issue_id, None)
                return
            operator_outcome = self._operator_outcomes.pop(issue_id, None)
            if operator_outcome:
                if self.persistence:
                    await self.persistence.finish_run(
                        entry,
                        status="cancelled",
                        error=f"run {operator_outcome} by operator",
                    )
                    from tempo_web.models import AgentRun

                    status = (
                        AgentRun.Status.PAUSED
                        if operator_outcome == "paused"
                        else AgentRun.Status.CANCELLED
                    )
                    await self.persistence.set_control_state(
                        entry.run_record_id,
                        status=status,
                        phase=operator_outcome.title(),
                    )
                self.claimed.discard(issue_id)
                self.retries.pop(issue_id, None)
                self._publish_live_state()
                return
            _, config = self.store.current()
            if task.cancelled():
                error = "worker cancelled or stalled"
                next_attempt = (entry.attempt or 0) + 1
                retry_exhausted = next_attempt > config.agent.max_retries
                if retry_exhausted:
                    entry.phase = "SafetyLimitReached"
                if self.persistence:
                    await self.persistence.finish_run(
                        entry,
                        status="cancelled",
                        error=error,
                    )
                if retry_exhausted:
                    self.claimed.discard(issue_id)
                    self.safety_blocked.add(issue_id)
                else:
                    await self._schedule_retry(
                        issue_id,
                        entry.issue.identifier,
                        next_attempt,
                        error,
                        config,
                        run_record_id=entry.run_record_id,
                    )
                return
            error = task.exception()
            if error is None:
                if self.persistence:
                    await self.persistence.finish_run(
                        entry,
                        status="succeeded",
                        error=None,
                    )
                self.completed.add(issue_id)
            else:
                category = getattr(error, "category", "")
                next_attempt = (entry.attempt or 0) + 1
                safety_limit = category in {
                    "token_budget_exceeded",
                    "validation_attempt_limit",
                }
                retry_exhausted = next_attempt > config.agent.max_retries
                if safety_limit or retry_exhausted:
                    entry.phase = "SafetyLimitReached"
                if self.persistence:
                    await self.persistence.finish_run(
                        entry,
                        status="failed",
                        error=str(error),
                    )
                if safety_limit or retry_exhausted:
                    self.claimed.discard(issue_id)
                    self.safety_blocked.add(issue_id)
                else:
                    await self._schedule_retry(
                        issue_id,
                        entry.issue.identifier,
                        next_attempt,
                        str(error),
                        config,
                        run_record_id=entry.run_record_id,
                    )
                await log.aerror(
                    "worker_failed",
                    issue_id=issue_id,
                    issue_identifier=entry.issue.identifier,
                    error=str(error),
                )
        self._refresh.set()
        self._publish_live_state()
        if not self.running and self._retired_trackers:
            retired, self._retired_trackers = self._retired_trackers, []
            for tracker in retired:
                await tracker.close()

    async def _schedule_retry(
        self,
        issue_id: str,
        identifier: str,
        attempt: int,
        error: str | None,
        config: ServiceConfig,
        *,
        continuation: bool = False,
        run_record_id: int | None = None,
    ) -> None:
        delay_ms = (
            1000
            if continuation
            else min(
                10_000 * (2 ** min(max(attempt - 1, 0), 20)),
                config.agent.max_retry_backoff_ms,
            )
        )
        due_at = utcnow() + timedelta(milliseconds=delay_ms)
        self.retries[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at=due_at,
            error=error,
            run_record_id=run_record_id,
        )
        self.claimed.add(issue_id)
        if self.persistence and run_record_id:
            await self.persistence.schedule_retry(
                run_record_id,
                attempt=attempt,
                due_at=due_at,
                error=error,
            )

    async def control_run(
        self,
        run_id: int,
        action: str,
        payload: dict[str, Any],
        *,
        user_id: int,
        idempotency_key: str,
    ) -> tuple[bool, str]:
        if not self.persistence:
            return False, "persistence_unavailable"
        from tempo_web.models import AgentRun, OperatorAction

        context = await self.persistence.run_control_context(run_id)
        if not context:
            return False, "run_not_found"
        issue_id = context["issue_id"]
        entry = self.running.get(issue_id)
        valid_actions = {
            "pause",
            "resume",
            "cancel",
            "retry",
            "requeue",
            "unblock",
            "reprioritize",
            "feedback",
        }
        if action not in valid_actions:
            return False, "unsupported_action"
        message = ""
        if action in {"pause", "cancel"}:
            if not entry and action == "pause":
                return False, "run_not_active"
            if entry:
                self._operator_outcomes[issue_id] = "paused" if action == "pause" else "cancelled"
                entry.task.cancel()
                message = f"{action} requested"
            else:
                await self.persistence.set_control_state(
                    run_id,
                    status=AgentRun.Status.CANCELLED,
                    phase="CancelledByOperator",
                )
                self.claimed.discard(issue_id)
                self.retries.pop(issue_id, None)
                self.safety_blocked.discard(issue_id)
                message = "run cancelled"
        elif action in {"resume", "retry", "requeue", "unblock"}:
            if entry:
                return False, "run_already_active"
            attempt = context["attempt"] + (1 if action == "retry" else 0)
            self.safety_blocked.discard(issue_id)
            self.completed.discard(issue_id)
            due_at = utcnow()
            await self.persistence.set_control_state(
                run_id,
                status=AgentRun.Status.RETRY_SCHEDULED,
                phase="RequeuedByOperator",
                available_at=due_at,
            )
            self.retries[issue_id] = RetryEntry(
                issue_id=issue_id,
                identifier=context["identifier"],
                attempt=attempt,
                due_at=due_at,
                error=None,
                run_record_id=run_id,
            )
            self.claimed.add(issue_id)
            self._refresh.set()
            message = "run queued"
        elif action == "reprioritize":
            priority = payload.get("priority")
            if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
                return False, "priority_must_be_a_positive_integer"
            await self.persistence.set_control_state(run_id, priority=priority)
            message = f"priority set to {priority}"
        elif action == "feedback":
            feedback = str(payload.get("message", "")).strip()
            if not feedback:
                return False, "feedback_message_required"
            previous = str(context.get("feedback", "")).strip()
            combined = f"{previous}\n{feedback}".strip()
            await self.persistence.set_control_state(run_id, feedback=combined)
            self._feedback[issue_id] = combined
            message = "feedback queued for the next turn"
        await self.persistence.record_operator_action(
            run_id,
            action=action,
            payload=payload,
            user_id=user_id,
            idempotency_key=idempotency_key,
            status=OperatorAction.Status.APPLIED,
            message=message,
        )
        self._publish_live_state()
        return True, message

    def snapshot(self) -> dict[str, Any]:
        _, config = self.store.current()
        now = utcnow()
        state_counts = Counter(
            normalize_state(entry.issue.state) for entry in self.running.values()
        )
        live_totals = {
            **self.totals.__dict__,
            "input_tokens": self.totals.input_tokens
            + sum(entry.session.codex_input_tokens for entry in self.running.values()),
            "output_tokens": self.totals.output_tokens
            + sum(entry.session.codex_output_tokens for entry in self.running.values()),
            "total_tokens": self.totals.total_tokens
            + sum(entry.session.codex_total_tokens for entry in self.running.values()),
            "runtime_seconds": self.totals.runtime_seconds
            + sum((now - entry.started_at).total_seconds() for entry in self.running.values()),
        }
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
                "project": (f"{config.project.organization}/{config.project.slug}"),
                "environment": config.project.environment,
                "workflow_graph": config.workflow.name,
                "runtime_kinds": sorted(
                    {provider.kind for provider in config.runtime_providers.values()}
                ),
            },
            "running": [
                {
                    "issue_id": issue_id,
                    "run_id": entry.run_record_id,
                    "project": f"{config.project.organization}/{config.project.slug}",
                    "identifier": entry.issue.identifier,
                    "title": entry.issue.title,
                    "state": entry.issue.state,
                    "url": entry.issue.url,
                    "attempt": entry.attempt,
                    "phase": entry.phase,
                    "started_at": entry.started_at.isoformat(),
                    "max_tokens": config.agent.max_tokens_per_run,
                    "max_validation_attempts": config.validation.max_attempts_per_run,
                    "graph": [
                        {
                            **state.__dict__,
                            "started_at": (
                                state.started_at.isoformat() if state.started_at else None
                            ),
                            "finished_at": (
                                state.finished_at.isoformat() if state.finished_at else None
                            ),
                        }
                        for state in entry.graph_nodes.values()
                    ],
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
                    "run_id": entry.run_record_id,
                    "project": f"{config.project.organization}/{config.project.slug}",
                }
                for entry in self.retries.values()
            ],
            "claimed_count": len(self.claimed),
            "completed_count": len(self.completed),
            "safety_blocked_count": len(self.safety_blocked),
            "running_by_state": dict(state_counts),
            "totals": live_totals,
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
                "max_tokens_per_run": config.agent.max_tokens_per_run,
                "max_retries": config.agent.max_retries,
                "per_state": config.agent.max_concurrent_agents_by_state,
                "max_retry_backoff_ms": config.agent.max_retry_backoff_ms,
                "profiles": {
                    name: {
                        "role": profile.role,
                        "runtime": profile.runtime,
                        "model": profile.model,
                        "completion": profile.completion,
                    }
                    for name, profile in config.agents.items()
                },
            },
            "platform": {
                "runtime_providers": {
                    name: {"kind": provider.kind}
                    for name, provider in config.runtime_providers.items()
                },
                "model_providers": {
                    name: {"kind": provider.kind, "model": provider.model}
                    for name, provider in config.model_providers.items()
                },
                "tool_providers": {
                    name: {"kind": provider.kind, "tool_count": len(provider.tools)}
                    for name, provider in config.tool_providers.items()
                },
                "workflow": {
                    "name": config.workflow.name,
                    "node_count": len(config.workflow.nodes),
                    "edge_count": len(config.workflow.edges),
                },
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
                "max_attempts_per_run": config.validation.max_attempts_per_run,
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
                "safety_blocked": len(self.safety_blocked),
                "rate_limits": self.rate_limits,
            },
        }

    def platform_snapshot(self) -> dict[str, Any]:
        _, config = self.store.current()
        return {
            "key": f"{config.project.organization}/{config.project.slug}",
            "name": config.project.name,
            "workflow_path": str(self.store.path),
            "runtime_providers": {
                name: provider.model_dump(mode="json")
                for name, provider in config.runtime_providers.items()
            },
            "model_providers": {
                name: provider.model_dump(mode="json")
                for name, provider in config.model_providers.items()
            },
            "tool_providers": {
                name: provider.model_dump(mode="json")
                for name, provider in config.tool_providers.items()
            },
            "agents": {
                name: profile.model_dump(mode="json") for name, profile in config.agents.items()
            },
            "workflow": config.workflow.model_dump(mode="json", by_alias=True),
            "last_reload_error": self.store.last_error,
        }

    async def update_platform_config(self, sections: dict[str, Any]) -> None:
        config = await self.store.update_platform_sections(sections)
        if not self.persistence:
            raise RuntimeError("persistence_unavailable")
        self._workflow_config_updated_at = await self.persistence.save_workflow_configuration(
            self.store.managed_sections
        )
        self.persistence.config = config
        await self.persistence.initialize()
        self._refresh.set()

    def issue_snapshot(self, identifier: str) -> dict[str, Any] | None:
        snapshot = self.snapshot()
        for category in ("running", "retries"):
            for row in snapshot[category]:
                if row["identifier"] == identifier:
                    return {"status": category.rstrip("s"), **row}
        return None
