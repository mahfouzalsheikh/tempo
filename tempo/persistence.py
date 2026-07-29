from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Count, Q, Sum

from .domain import Issue, RunningEntry, Totals, utcnow


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
        checksum = hashlib.sha256(workflow_bytes).hexdigest()
        latest = (
            await WorkflowVersion.objects.filter(
                project=project,
                name="issue-to-pull-request",
            )
            .order_by("-version")
            .afirst()
        )
        workflow, _ = await WorkflowVersion.objects.aget_or_create(
            project=project,
            checksum=checksum,
            defaults={
                "name": "issue-to-pull-request",
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
        ).filter(Q(pull_request_url__gt="") | Q(phase="NoChangesRequired"))
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

    async def record_event(self, entry: RunningEntry, event: dict[str, Any]) -> None:
        if not entry.run_record_id:
            return
        from tempo_web.models import AgentRun, AgentSession, ValidationAttempt, ValidationCommand

        session = entry.session
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
