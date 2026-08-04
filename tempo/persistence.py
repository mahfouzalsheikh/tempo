from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Count, Q, Sum

from .domain import Issue, RunningEntry, Totals, utcnow

if TYPE_CHECKING:
    from .agent_runtime import RuntimeResumeContext


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class PersistenceStore:
    """Database-backed execution queue, leases, checkpoints, and runtime history."""

    def __init__(
        self, tracker_kind: str, *, config: Any = None, workflow_path: Path | None = None
    ) -> None:
        self.tracker_kind = tracker_kind
        self.config = config
        self.workflow_path = workflow_path
        self.project_id: int | None = None
        self.environment_id: int | None = None
        self.workflow_version_id: int | None = None

    async def initialize(self) -> None:
        from tempo_web.models import Environment, Organization, Project, Repository, WorkflowVersion

        project_config = getattr(self.config, "project", None)
        organization_slug = getattr(project_config, "organization", "default")
        project_slug = getattr(project_config, "slug", "default")
        project_name = getattr(project_config, "name", "Default project")
        environment_slug = getattr(project_config, "environment", "development")
        project_limit = getattr(project_config, "max_concurrent_runs", 3)
        environment_limit = getattr(project_config, "environment_max_concurrent_runs", 3)
        organization, _ = await Organization.objects.aget_or_create(
            slug=organization_slug,
            defaults={"name": organization_slug.replace("-", " ").title()},
        )
        project, _ = await Project.objects.aupdate_or_create(
            organization=organization,
            slug=project_slug,
            defaults={
                "name": project_name,
                "active": True,
                "max_concurrent_runs": project_limit,
            },
        )
        environment, _ = await Environment.objects.aupdate_or_create(
            project=project,
            slug=environment_slug,
            defaults={
                "name": environment_slug.replace("-", " ").title(),
                "workspace_root": str(getattr(getattr(self.config, "workspace", None), "root", "")),
                "max_concurrent_runs": environment_limit,
                "active": True,
            },
        )
        provider_ref = ""
        provider = getattr(getattr(self.config, "tracker", None), "provider", {}) or {}
        if self.tracker_kind == "github":
            provider_ref = str(provider.get("repo", ""))
        if provider_ref:
            await Repository.objects.aupdate_or_create(
                project=project,
                provider=self.tracker_kind,
                external_ref=provider_ref,
                defaults={
                    "clone_url": f"https://github.com/{provider_ref}.git",
                    "active": True,
                },
            )
        workflow_path = self.workflow_path
        workflow_bytes = b""
        if workflow_path and workflow_path.exists():
            workflow_bytes = await sync_to_async(workflow_path.read_bytes, thread_sensitive=False)()
        workflow_name = getattr(getattr(self.config, "workflow", None), "name", None)
        workflow_name = workflow_name or "issue-to-pull-request"
        platform_payload = {}
        if hasattr(self.config, "runtime_providers"):
            dumped_config = self.config.model_dump(mode="json")
            platform_payload = {
                key: _json_safe(dumped_config[key])
                for key in (
                    "runtime_providers",
                    "model_providers",
                    "tool_providers",
                    "agents",
                    "workflow",
                )
            }
        checksum = hashlib.sha256(
            workflow_bytes + json.dumps(platform_payload, sort_keys=True).encode()
        ).hexdigest()
        latest = (
            await WorkflowVersion.objects.filter(
                project=project,
                name=workflow_name,
            )
            .order_by("-version")
            .afirst()
        )
        workflow, _ = await WorkflowVersion.objects.aget_or_create(
            project=project,
            checksum=checksum,
            defaults={
                "name": workflow_name,
                "version": (latest.version + 1) if latest else 1,
                "path": str(workflow_path or ""),
                "config": _json_safe(
                    self.config.model_dump(mode="json") if self.config is not None else {}
                ),
                "active": True,
            },
        )
        self.project_id = project.pk
        self.environment_id = environment.pk
        self.workflow_version_id = workflow.pk
        await self._ensure_workflow_configuration(platform_payload)
        await self._sync_platform_definitions()

    async def workflow_configuration(self) -> tuple[dict[str, Any], Any | None]:
        if self.project_id is None:
            return {}, None
        from tempo_web.models import WorkflowConfiguration

        row = await WorkflowConfiguration.objects.filter(
            project_id=self.project_id, active=True
        ).afirst()
        return (_json_safe(row.configuration), row.updated_at) if row else ({}, None)

    async def save_workflow_configuration(self, sections: dict[str, Any]) -> Any:
        if self.project_id is None:
            raise RuntimeError("persistence store is not initialized")
        from tempo_web.models import WorkflowConfiguration

        row, created = await WorkflowConfiguration.objects.aget_or_create(
            project_id=self.project_id,
            defaults={"configuration": _json_safe(sections)},
        )
        if not created:
            row.configuration = _json_safe(sections)
            row.active = True
            row.revision += 1
            await row.asave(update_fields=["configuration", "active", "revision", "updated_at"])
        return row.updated_at

    async def _ensure_workflow_configuration(self, sections: dict[str, Any]) -> None:
        if self.project_id is None or not sections:
            return
        from tempo_web.models import WorkflowConfiguration

        await WorkflowConfiguration.objects.aget_or_create(
            project_id=self.project_id,
            defaults={"configuration": _json_safe(sections)},
        )

    @sync_to_async(thread_sensitive=True)
    def _sync_platform_definitions(self) -> None:
        if (
            self.config is None
            or self.project_id is None
            or self.workflow_version_id is None
            or not hasattr(self.config, "runtime_providers")
        ):
            return
        from tempo_web.models import (
            AgentProfile,
            AgentRuntimeDefinition,
            ModelProviderDefinition,
            ToolProviderDefinition,
            WorkflowEdgeDefinition,
            WorkflowNodeDefinition,
        )

        runtimes = {}
        for name, config in self.config.runtime_providers.items():
            runtimes[name], _ = AgentRuntimeDefinition.objects.update_or_create(
                project_id=self.project_id,
                name=name,
                defaults={
                    "kind": config.kind,
                    "configuration": {
                        "command": config.command,
                        "settings": _json_safe(config.settings),
                    },
                    "active": True,
                },
            )
        models = {}
        for name, config in self.config.model_providers.items():
            models[name], _ = ModelProviderDefinition.objects.update_or_create(
                project_id=self.project_id,
                name=name,
                defaults={
                    "kind": config.kind,
                    "model": config.model or "",
                    "configuration": {
                        "fallbacks": _json_safe(config.fallbacks),
                        "routes": _json_safe(
                            [route.model_dump(mode="json") for route in config.routes]
                        ),
                        "settings": _json_safe(config.settings),
                    },
                    "active": True,
                },
            )
        tools = {}
        for name, config in self.config.tool_providers.items():
            tools[name], _ = ToolProviderDefinition.objects.update_or_create(
                project_id=self.project_id,
                name=name,
                defaults={
                    "kind": config.kind,
                    "tools": _json_safe(config.tools),
                    "configuration": {
                        "allow_all": config.allow_all,
                        "settings": _json_safe(config.settings),
                    },
                    "active": True,
                },
            )
        agents = {}
        for name, config in self.config.agents.items():
            profile, _ = AgentProfile.objects.update_or_create(
                project_id=self.project_id,
                name=name,
                defaults={
                    "role": config.role,
                    "runtime": runtimes[config.runtime],
                    "model_provider": models[config.model],
                    "prompt": config.prompt,
                    "max_turns": config.max_turns,
                    "completion": config.completion,
                    "configuration": {
                        "capabilities": _json_safe(config.capabilities),
                        "max_model_cost_per_million_tokens": (
                            config.max_model_cost_per_million_tokens
                        ),
                        "settings": _json_safe(config.settings),
                    },
                    "active": True,
                },
            )
            profile.tool_providers.set([tools[item] for item in config.tool_providers])
            agents[name] = profile
        node_rows = {}
        for position, node in enumerate(self.config.workflow.nodes):
            row, _ = WorkflowNodeDefinition.objects.update_or_create(
                workflow_version_id=self.workflow_version_id,
                key=node.id,
                defaults={
                    "name": node.name or node.id.replace("-", " ").replace("_", " ").title(),
                    "node_type": node.type,
                    "agent_profile": agents.get(node.agent or ""),
                    "prompt": node.prompt,
                    "max_retries": node.max_retries,
                    "configuration": {
                        "approval_message": node.approval_message,
                        **_json_safe(node.settings),
                    },
                    "position": position,
                },
            )
            node_rows[node.id] = row
        WorkflowEdgeDefinition.objects.filter(
            workflow_version_id=self.workflow_version_id
        ).delete()
        WorkflowEdgeDefinition.objects.bulk_create(
            [
                WorkflowEdgeDefinition(
                    workflow_version_id=self.workflow_version_id,
                    source=node_rows[edge.source],
                    target=node_rows[edge.target],
                    condition=edge.condition,
                    position=position,
                )
                for position, edge in enumerate(self.config.workflow.edges)
            ]
        )

    async def reconcile_incomplete_records(self) -> None:
        from tempo_web.models import AgentRun, ValidationAttempt

        now = utcnow()
        await (
            AgentRun.objects.filter(
                project_id=self.project_id,
                status=AgentRun.Status.RUNNING,
            )
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .aupdate(
                status=AgentRun.Status.RETRY_SCHEDULED,
                phase="Recovering",
                available_at=now,
                worker_id="",
                lease_token="",
                lease_expires_at=None,
                error="Worker lease expired; resuming from the last durable checkpoint.",
            )
        )
        await AgentRun.objects.filter(
            project_id=self.project_id,
            status=AgentRun.Status.WAITING_APPROVAL,
            lease_expires_at__lte=now,
        ).aupdate(
            worker_id="",
            lease_token="",
            lease_expires_at=None,
            heartbeat_at=None,
            phase="WaitingForApproval",
        )
        await ValidationAttempt.objects.filter(
            run__project_id=self.project_id,
            run__status=AgentRun.Status.RETRY_SCHEDULED,
            status=ValidationAttempt.Status.RUNNING,
        ).aupdate(
            status=ValidationAttempt.Status.INVALIDATED,
            finished_at=now,
        )

    async def runtime_summary(self) -> tuple[Totals, int]:
        """Load durable headline totals without making the database the scheduler."""
        from tempo_web.models import AgentRun, ValidationAttempt

        runs = AgentRun.objects.filter(project_id=self.project_id)
        aggregates = await runs.aaggregate(
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            total_tokens=Sum("total_tokens"),
            completed_runs=Count("id", filter=Q(status=AgentRun.Status.SUCCEEDED)),
        )
        validations = await ValidationAttempt.objects.filter(
            run__project_id=self.project_id
        ).aaggregate(
            passes=Count("id", filter=Q(status=ValidationAttempt.Status.PASSED)),
            failures=Count("id", filter=Q(status=ValidationAttempt.Status.FAILED)),
            validated_runs=Count(
                "run_id",
                distinct=True,
                filter=Q(status=ValidationAttempt.Status.PASSED),
            ),
        )
        runtime = timedelta()
        async for started_at, finished_at in runs.exclude(finished_at=None).values_list(
            "started_at", "finished_at"
        ):
            runtime += finished_at - started_at
        return (
            Totals(
                input_tokens=int(aggregates["input_tokens"] or 0),
                output_tokens=int(aggregates["output_tokens"] or 0),
                total_tokens=int(aggregates["total_tokens"] or 0),
                runtime_seconds=runtime.total_seconds(),
                validation_passes=int(validations["passes"] or 0),
                validation_failures=int(validations["failures"] or 0),
                validated_runs=int(validations["validated_runs"] or 0),
            ),
            int(aggregates["completed_runs"] or 0),
        )

    async def completed_issue_ids(self) -> set[str]:
        from tempo_web.models import AgentRun

        rows = AgentRun.objects.filter(
            project_id=self.project_id,
            status=AgentRun.Status.SUCCEEDED,
        ).filter(
            Q(pull_request_url__gt="")
            | Q(phase="NoChangesRequired")
            | Q(phase="WorkflowCompleted")
        )
        return {
            external_id async for external_id in rows.values_list("issue__external_id", flat=True)
        }

    async def safety_blocked_issue_ids(self) -> set[str]:
        from tempo_web.models import AgentRun

        rows = AgentRun.objects.filter(
            project_id=self.project_id,
            phase="SafetyLimitReached",
        )
        return {
            external_id async for external_id in rows.values_list("issue__external_id", flat=True)
        }

    async def start_run(
        self,
        entry: RunningEntry,
        workspace_path: Path,
    ) -> int:
        from tempo_web.models import AgentRun, AgentSession, TrackedIssue

        if self.project_id is None:
            await self.initialize()
        issue, _ = await TrackedIssue.objects.aupdate_or_create(
            project_id=self.project_id,
            tracker_kind=self.tracker_kind,
            external_id=entry.issue.id,
            defaults=self._issue_defaults(entry.issue),
        )
        if entry.run_record_id:
            run = await AgentRun.objects.aget(pk=entry.run_record_id)
            await AgentRun.objects.filter(pk=run.pk).aupdate(
                status=AgentRun.Status.RUNNING,
                phase=entry.phase,
                workspace_path=str(workspace_path),
                started_at=entry.started_at,
                finished_at=None,
                heartbeat_at=utcnow(),
            )
        else:
            run = await AgentRun.objects.acreate(
                project_id=self.project_id,
                environment_id=self.environment_id,
                workflow_version_id=self.workflow_version_id,
                issue=issue,
                attempt=entry.attempt,
                phase=entry.phase,
                status=AgentRun.Status.RUNNING,
                workspace_path=str(workspace_path),
                started_at=entry.started_at,
                heartbeat_at=utcnow(),
            )
        await AgentSession.objects.aget_or_create(run=run)
        return run.pk

    async def resume_context(self, run_id: int) -> dict[str, Any] | None:
        """Return the durable Codex thread state needed for a continuation attempt."""
        from tempo_web.models import AgentRun, AgentSession, RunCheckpoint

        session = await AgentSession.objects.select_related("run").filter(run_id=run_id).afirst()
        if not session or not session.thread_id:
            return None
        pull_request_url = session.run.pull_request_url
        pull_request_number: int | None = None
        if pull_request_url:
            with contextlib.suppress(ValueError):
                pull_request_number = int(pull_request_url.rstrip("/").rsplit("/", 1)[-1])
        if not pull_request_url:
            checkpoints = RunCheckpoint.objects.filter(
                run_id=run_id,
                kind="tool_call_completed",
            ).order_by("-sequence")
            async for checkpoint in checkpoints:
                recovered = self._pull_request_from_checkpoint(checkpoint.payload)
                if recovered:
                    pull_request_url, pull_request_number = recovered
                    await AgentRun.objects.filter(pk=run_id).aupdate(
                        pull_request_url=pull_request_url
                    )
                    break
        return {
            "thread_id": session.thread_id,
            "agent_role": session.agent_role,
            "turn_count": session.turn_count,
            "usage_baseline": {
                "input_tokens": session.thread_input_tokens,
                "output_tokens": session.thread_output_tokens,
                "total_tokens": session.thread_total_tokens,
            },
            "last_event": session.last_event,
            "last_message": session.last_message,
            "pull_request_url": pull_request_url,
            "pull_request_number": pull_request_number,
        }

    @staticmethod
    def _pull_request_from_checkpoint(
        payload: dict[str, Any],
    ) -> tuple[str, int] | None:
        if payload.get("tool") != "github_api" or not payload.get("success"):
            return None
        arguments = payload.get("arguments") or {}
        method = str(arguments.get("method", "GET")).upper()
        path = str(arguments.get("path", "")).rstrip("/")
        if method not in {"GET", "POST"}:
            return None
        collection_match = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls", path)
        numbered_match = re.fullmatch(
            r"/repos/([^/]+)/([^/]+)/pulls/(\d+)(?:/.*)?",
            path,
        )
        if not collection_match and not numbered_match:
            return None
        try:
            output = json.loads(str(payload.get("output", "")))
        except json.JSONDecodeError:
            output = None
        if output is not None:
            candidates = output if isinstance(output, list) else [output]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                url = str(candidate.get("html_url", ""))
                number = candidate.get("number")
                if url and isinstance(number, int) and candidate.get("state", "open") == "open":
                    return url, number
            if (
                numbered_match
                and isinstance(output, dict)
                and output.get("state") not in {None, "open"}
            ):
                return None
        if method == "GET" and numbered_match:
            owner, repository, number_text = numbered_match.groups()
            return (
                f"https://github.com/{owner}/{repository}/pull/{number_text}",
                int(number_text),
            )
        return None

    async def initialize_run_nodes(self, entry: RunningEntry) -> None:
        if not entry.run_record_id or self.config is None:
            return
        from tempo_web.models import RunNode, WorkflowNodeDefinition

        definitions = {
            row.key: row
            async for row in WorkflowNodeDefinition.objects.filter(
                workflow_version_id=self.workflow_version_id
            )
        }
        incoming: dict[str, list[str]] = {node.id: [] for node in self.config.workflow.nodes}
        for edge in self.config.workflow.edges:
            incoming[edge.target].append(edge.source)
        for node in self.config.workflow.nodes:
            profile = self.config.agents.get(node.agent or "")
            row, _ = await RunNode.objects.aget_or_create(
                run_id=entry.run_record_id,
                node_key=node.id,
                defaults={
                    "node_definition": definitions.get(node.id),
                    "name": node.name or node.id.replace("-", " ").replace("_", " ").title(),
                    "node_type": node.type,
                    "agent_name": node.agent or "",
                    "role": profile.role if profile else "",
                    "runtime": profile.runtime if profile else "",
                    "model": (
                        self.config.model_providers[profile.model].model or profile.model
                        if profile
                        else ""
                    ),
                    "dependencies": incoming[node.id],
                },
            )
            state = entry.graph_nodes[node.id]
            state.status = row.status
            state.attempt = row.attempt
            state.started_at = row.started_at
            state.finished_at = row.finished_at
            state.error = row.error or None
            state.output = row.output

    async def start_run_node(self, run_id: int, node_id: str, attempt: int) -> None:
        from tempo_web.models import RunNode

        await RunNode.objects.filter(run_id=run_id, node_key=node_id).aupdate(
            status=RunNode.Status.RUNNING,
            attempt=attempt,
            started_at=utcnow(),
            finished_at=None,
            error="",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )

    async def run_node_resume_context(
        self,
        run_id: int,
        node_id: str,
    ) -> RuntimeResumeContext | None:
        """Return the durable runtime thread state for an interrupted graph node."""
        from tempo_web.models import RunNode

        from .agent_runtime import RuntimeResumeContext

        row = await RunNode.objects.filter(run_id=run_id, node_key=node_id).afirst()
        if not row or not row.thread_id:
            return None
        return RuntimeResumeContext(
            thread_id=row.thread_id,
            usage_baseline={
                "input_tokens": row.thread_input_tokens,
                "output_tokens": row.thread_output_tokens,
                "total_tokens": row.thread_total_tokens,
            },
        )

    async def set_run_node_model(self, run_id: int, node_id: str, model: str) -> None:
        from tempo_web.models import RunNode

        await RunNode.objects.filter(run_id=run_id, node_key=node_id).aupdate(model=model)

    async def finish_run_node(
        self,
        run_id: int,
        node_id: str,
        *,
        status: str,
        error: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> None:
        from tempo_web.models import RunNode

        await RunNode.objects.filter(run_id=run_id, node_key=node_id).aupdate(
            status=status,
            finished_at=utcnow(),
            error=error or "",
            output=_json_safe(output or {}),
            checkpoint={"status": status, "finished_at": utcnow().isoformat()},
        )

    async def record_run_node_event(
        self,
        run_id: int,
        node_id: str,
        session: Any,
    ) -> None:
        from tempo_web.models import RunNode

        await RunNode.objects.filter(run_id=run_id, node_key=node_id).aupdate(
            session_id=session.session_id or "",
            thread_id=session.thread_id or "",
            turn_count=session.turn_count,
            input_tokens=session.codex_input_tokens,
            output_tokens=session.codex_output_tokens,
            total_tokens=session.codex_total_tokens,
            thread_input_tokens=session.thread_input_tokens,
            thread_output_tokens=session.thread_output_tokens,
            thread_total_tokens=session.thread_total_tokens,
        )

    async def enqueue_issue(self, issue: Issue, *, attempt: int | None = None) -> int | None:
        """Accept work durably before a worker process is launched."""
        if self.project_id is None:
            await self.initialize()
        from tempo_web.models import AgentRun, TrackedIssue

        tracked, _ = await TrackedIssue.objects.aupdate_or_create(
            project_id=self.project_id,
            tracker_kind=self.tracker_kind,
            external_id=issue.id,
            defaults=self._issue_defaults(issue),
        )
        key = f"{self.project_id}:{self.tracker_kind}:{issue.id}"
        existing = await AgentRun.objects.filter(idempotency_key=key).afirst()
        if existing:
            if existing.status in {
                AgentRun.Status.QUEUED,
                AgentRun.Status.RETRY_SCHEDULED,
                AgentRun.Status.RUNNING,
                AgentRun.Status.PAUSED,
                AgentRun.Status.WAITING_APPROVAL,
            }:
                return existing.pk
            return None
        run = await AgentRun.objects.acreate(
            project_id=self.project_id,
            environment_id=self.environment_id,
            workflow_version_id=self.workflow_version_id,
            issue=tracked,
            idempotency_key=key,
            attempt=attempt,
            priority=issue.priority if issue.priority in {1, 2, 3, 4} else 5,
            phase="Queued",
            status=AgentRun.Status.QUEUED,
            available_at=utcnow(),
            started_at=utcnow(),
        )
        return run.pk

    @sync_to_async(thread_sensitive=True)
    def claim_run(self, run_id: int, worker_id: str, *, lease_seconds: int = 30) -> bool:
        from tempo_web.models import AgentRun, Environment, Project, WorkerLease

        now = utcnow()
        with transaction.atomic():
            run = AgentRun.objects.select_for_update().get(pk=run_id)
            claimable = run.status in {
                AgentRun.Status.QUEUED,
                AgentRun.Status.RETRY_SCHEDULED,
            } and (run.available_at is None or run.available_at <= now)
            stale = (
                run.status == AgentRun.Status.RUNNING
                and run.lease_expires_at is not None
                and run.lease_expires_at <= now
            )
            if not claimable and not stale:
                return False
            project = Project.objects.select_for_update().get(pk=run.project_id)
            environment = (
                Environment.objects.select_for_update().get(pk=run.environment_id)
                if run.environment_id
                else None
            )
            active = AgentRun.objects.filter(
                project=project,
                status__in=[
                    AgentRun.Status.RUNNING,
                    AgentRun.Status.WAITING_APPROVAL,
                ],
            ).exclude(pk=run.pk)
            if active.count() >= project.max_concurrent_runs:
                return False
            if (
                environment
                and active.filter(environment=environment).count()
                >= environment.max_concurrent_runs
            ):
                return False
            token = uuid.uuid4().hex
            expires_at = now + timedelta(seconds=lease_seconds)
            run.status = AgentRun.Status.RUNNING
            run.phase = "Claimed"
            run.worker_id = worker_id
            run.lease_token = token
            run.heartbeat_at = now
            run.lease_expires_at = expires_at
            run.save(
                update_fields=[
                    "status",
                    "phase",
                    "worker_id",
                    "lease_token",
                    "heartbeat_at",
                    "lease_expires_at",
                ]
            )
            WorkerLease.objects.create(
                run=run,
                worker_id=worker_id,
                token=token,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=expires_at,
            )
            return True

    async def heartbeat(self, run_id: int, *, lease_seconds: int = 30) -> None:
        from tempo_web.models import AgentRun, WorkerLease

        now = utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
        run = await AgentRun.objects.filter(
            pk=run_id,
            status__in=[AgentRun.Status.RUNNING, AgentRun.Status.WAITING_APPROVAL],
        ).afirst()
        if not run:
            return
        await AgentRun.objects.filter(pk=run_id).aupdate(
            heartbeat_at=now,
            lease_expires_at=expires_at,
        )
        if run.lease_token:
            await WorkerLease.objects.filter(token=run.lease_token, released_at=None).aupdate(
                heartbeat_at=now,
                expires_at=expires_at,
            )

    async def checkpoint(
        self,
        run_id: int,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> None:
        from tempo_web.models import AgentRun, RunCheckpoint

        sequence = await RunCheckpoint.objects.filter(run_id=run_id).acount() + 1
        checkpoint, _ = await RunCheckpoint.objects.aget_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "run_id": run_id,
                "sequence": sequence,
                "kind": kind,
                "payload": _json_safe(payload),
            },
        )
        await AgentRun.objects.filter(pk=run_id).aupdate(
            checkpoint={
                "sequence": checkpoint.sequence,
                "kind": checkpoint.kind,
                "payload": checkpoint.payload,
            }
        )

    async def schedule_retry(
        self,
        run_id: int,
        *,
        attempt: int,
        due_at: Any,
        error: str | None,
    ) -> None:
        from tempo_web.models import AgentRun, WorkerLease

        run = await AgentRun.objects.aget(pk=run_id)
        await AgentRun.objects.filter(pk=run_id).aupdate(
            status=AgentRun.Status.RETRY_SCHEDULED,
            phase="RetryScheduled",
            attempt=attempt,
            available_at=due_at,
            worker_id="",
            lease_token="",
            lease_expires_at=None,
            error=error or "",
            finished_at=None,
        )
        if run.lease_token:
            await WorkerLease.objects.filter(token=run.lease_token, released_at=None).aupdate(
                released_at=utcnow()
            )

    async def pending_runs(self) -> list[dict[str, Any]]:
        from tempo_web.models import AgentRun

        rows = AgentRun.objects.filter(
            project_id=self.project_id,
            status__in=[AgentRun.Status.QUEUED, AgentRun.Status.RETRY_SCHEDULED],
        ).select_related("issue")
        return [
            {
                "run_id": row.pk,
                "issue_id": row.issue.external_id,
                "identifier": row.issue.identifier,
                "attempt": row.attempt,
                "due_at": row.available_at or utcnow(),
                "error": row.error or None,
                "feedback": row.feedback,
            }
            async for row in rows
        ]

    async def create_approval(
        self,
        run_id: int,
        *,
        request_key: str,
        kind: str,
        details: dict[str, Any],
    ) -> int:
        from tempo_web.models import AgentRun, ApprovalRequest

        approval, _ = await ApprovalRequest.objects.aget_or_create(
            request_key=request_key,
            defaults={
                "run_id": run_id,
                "kind": kind,
                "title": str(details.get("params", {}).get("reason") or kind)[:500],
                "details": _json_safe(details),
                "proposed_arguments": _json_safe(
                    details.get("params", {}).get("arguments") or details.get("params", {})
                ),
            },
        )
        await AgentRun.objects.filter(pk=run_id).aupdate(
            status=AgentRun.Status.WAITING_APPROVAL,
            phase="WaitingForApproval",
        )
        return approval.pk

    async def approval_decision(self, approval_id: int) -> dict[str, Any] | None:
        from tempo_web.models import ApprovalRequest

        approval = await ApprovalRequest.objects.filter(pk=approval_id).afirst()
        if not approval or approval.status == ApprovalRequest.Status.PENDING:
            return None
        return {
            "approved": approval.status == ApprovalRequest.Status.APPROVED,
            "edited_arguments": approval.edited_arguments,
            "note": approval.decision_note,
        }

    async def resume_after_approval(self, run_id: int) -> None:
        from tempo_web.models import AgentRun

        await AgentRun.objects.filter(
            pk=run_id,
            status=AgentRun.Status.WAITING_APPROVAL,
        ).aupdate(status=AgentRun.Status.RUNNING, phase="StreamingTurn")

    async def record_operator_action(
        self,
        run_id: int,
        *,
        action: str,
        payload: dict[str, Any],
        user_id: int,
        idempotency_key: str,
        status: str,
        message: str = "",
    ) -> None:
        from tempo_web.models import OperatorAction

        await OperatorAction.objects.aget_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "run_id": run_id,
                "action": action,
                "payload": _json_safe(payload),
                "requested_by_id": user_id,
                "status": status,
                "applied_at": utcnow() if status == OperatorAction.Status.APPLIED else None,
                "message": message,
            },
        )

    async def run_control_context(self, run_id: int) -> dict[str, Any] | None:
        from tempo_web.models import AgentRun

        run = (
            await AgentRun.objects.select_related("issue")
            .filter(
                pk=run_id,
                project_id=self.project_id,
            )
            .afirst()
        )
        if not run:
            return None
        return {
            "run_id": run.pk,
            "issue_id": run.issue.external_id,
            "identifier": run.issue.identifier,
            "status": run.status,
            "attempt": run.attempt or 0,
            "priority": run.priority,
            "error": run.error,
            "feedback": run.feedback,
        }

    async def set_control_state(
        self,
        run_id: int,
        *,
        status: str | None = None,
        phase: str | None = None,
        priority: int | None = None,
        feedback: str | None = None,
        available_at: Any = None,
        attempt: int | None = None,
    ) -> None:
        from tempo_web.models import AgentRun

        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
            if status in {AgentRun.Status.QUEUED, AgentRun.Status.RETRY_SCHEDULED}:
                updates["finished_at"] = None
        if phase is not None:
            updates["phase"] = phase
        if priority is not None:
            updates["priority"] = priority
        if feedback is not None:
            updates["feedback"] = feedback
        if available_at is not None:
            updates["available_at"] = available_at
        if attempt is not None:
            updates["attempt"] = attempt
        if status and status != AgentRun.Status.RUNNING:
            updates.update(
                {
                    "worker_id": "",
                    "lease_token": "",
                    "lease_expires_at": None,
                }
            )
        await AgentRun.objects.filter(pk=run_id, project_id=self.project_id).aupdate(**updates)

    async def sync_issue(self, issue: Issue) -> None:
        from tempo_web.models import TrackedIssue

        await TrackedIssue.objects.aupdate_or_create(
            project_id=self.project_id,
            tracker_kind=self.tracker_kind,
            external_id=issue.id,
            defaults=self._issue_defaults(issue),
        )

    async def record_event(
        self,
        entry: RunningEntry,
        event: dict[str, Any],
        *,
        live_session: Any = None,
    ) -> None:
        if not entry.run_record_id:
            return
        from tempo_web.models import AgentRun, AgentSession, ValidationAttempt, ValidationCommand

        session = live_session or entry.session
        await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
            phase=entry.phase,
            pull_request_url=session.pull_request_url or "",
            input_tokens=session.codex_input_tokens,
            output_tokens=session.codex_output_tokens,
            total_tokens=session.codex_total_tokens,
        )
        await AgentSession.objects.aupdate_or_create(
            run_id=entry.run_record_id,
            defaults={
                "agent_role": session.agent_role,
                "session_id": session.session_id or "",
                "thread_id": session.thread_id or "",
                "turn_id": session.turn_id or "",
                "process_id": session.codex_app_server_pid or "",
                "turn_count": session.turn_count,
                "last_event": session.last_codex_event or "",
                "last_event_at": session.last_codex_timestamp,
                "last_message": _json_safe(session.last_codex_message or {}),
                "input_tokens": session.codex_input_tokens,
                "output_tokens": session.codex_output_tokens,
                "total_tokens": session.codex_total_tokens,
                "thread_input_tokens": session.thread_input_tokens,
                "thread_output_tokens": session.thread_output_tokens,
                "thread_total_tokens": session.thread_total_tokens,
            },
        )

        event_name = event.get("event")
        if event_name == "validation_started":
            validation = await ValidationAttempt.objects.acreate(
                run_id=entry.run_record_id,
                status=ValidationAttempt.Status.RUNNING,
                summary=str(event.get("summary", "")),
                started_at=session.validation_started_at or utcnow(),
            )
            session.validation_record_id = validation.pk
        elif event_name == "validation_command_completed" and session.validation_record_id:
            position = await ValidationCommand.objects.filter(
                validation_id=session.validation_record_id
            ).acount()
            await ValidationCommand.objects.acreate(
                validation_id=session.validation_record_id,
                position=position + 1,
                name=str(event.get("name", "")),
                command=str(event.get("command", "")),
                exit_code=event.get("exit_code"),
                output=str(event.get("output", "")),
                cleanup=bool(event.get("cleanup")),
                started_at=session.last_codex_timestamp,
                finished_at=session.last_codex_timestamp,
            )
        elif event_name == "validation_completed" and session.validation_record_id:
            status = (
                ValidationAttempt.Status.PASSED
                if event.get("success")
                else ValidationAttempt.Status.FAILED
            )
            await ValidationAttempt.objects.filter(pk=session.validation_record_id).aupdate(
                status=status,
                finished_at=session.validation_finished_at or utcnow(),
            )
        elif event_name == "validation_invalidated" and session.validation_record_id:
            await ValidationAttempt.objects.filter(pk=session.validation_record_id).aupdate(
                status=ValidationAttempt.Status.INVALIDATED,
                finished_at=utcnow(),
            )
        elif event_name == "validation_fingerprint_recorded" and session.validation_record_id:
            await ValidationAttempt.objects.filter(pk=session.validation_record_id).aupdate(
                workspace_fingerprint=str(event.get("fingerprint", ""))
            )

    async def finish_run(
        self,
        entry: RunningEntry,
        *,
        status: str,
        error: str | None,
    ) -> None:
        if not entry.run_record_id:
            return
        from tempo_web.models import AgentRun, ValidationAttempt, WorkerLease

        await ValidationAttempt.objects.filter(
            run_id=entry.run_record_id,
            status=ValidationAttempt.Status.RUNNING,
        ).aupdate(
            status=ValidationAttempt.Status.INVALIDATED,
            finished_at=utcnow(),
        )

        run = await AgentRun.objects.aget(pk=entry.run_record_id)
        await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
            phase=entry.phase,
            status=status,
            finished_at=utcnow(),
            error=error or "",
            pull_request_url=entry.session.pull_request_url or "",
            input_tokens=entry.session.codex_input_tokens,
            output_tokens=entry.session.codex_output_tokens,
            total_tokens=entry.session.codex_total_tokens,
            lease_expires_at=None,
            lease_token="",
        )
        if run.lease_token:
            await WorkerLease.objects.filter(token=run.lease_token, released_at=None).aupdate(
                released_at=utcnow()
            )

    @staticmethod
    def _issue_defaults(issue: Issue) -> dict[str, Any]:
        return {
            "identifier": issue.identifier,
            "title": issue.title,
            "description": issue.description or "",
            "state": issue.state,
            "url": issue.url or "",
            "labels": issue.labels,
            "native_ref": _json_safe(issue.native_ref or {}),
        }
