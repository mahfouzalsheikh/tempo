from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.db.models import Count, Q, Sum

from .domain import Issue, RunningEntry, Totals, utcnow


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class PersistenceStore:
    """Persists operator-visible runtime history without acting as the scheduler queue."""

    def __init__(self, tracker_kind: str) -> None:
        self.tracker_kind = tracker_kind

    async def reconcile_incomplete_records(self) -> None:
        from tempo_web.models import AgentRun, ValidationAttempt

        now = utcnow()
        await AgentRun.objects.filter(
            issue__tracker_kind=self.tracker_kind,
            status=AgentRun.Status.RUNNING,
        ).aupdate(
            status=AgentRun.Status.CANCELLED,
            phase="Interrupted",
            finished_at=now,
            error="Tempo restarted before this run completed.",
        )
        await ValidationAttempt.objects.filter(
            run__issue__tracker_kind=self.tracker_kind,
            status=ValidationAttempt.Status.RUNNING,
        ).aupdate(
            status=ValidationAttempt.Status.INVALIDATED,
            finished_at=now,
        )

    async def runtime_summary(self) -> tuple[Totals, int]:
        """Load durable headline totals without making the database the scheduler."""
        from tempo_web.models import AgentRun, ValidationAttempt

        runs = AgentRun.objects.filter(issue__tracker_kind=self.tracker_kind)
        aggregates = await runs.aaggregate(
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            total_tokens=Sum("total_tokens"),
            completed_runs=Count("id", filter=Q(status=AgentRun.Status.SUCCEEDED)),
        )
        validations = await ValidationAttempt.objects.filter(
            run__issue__tracker_kind=self.tracker_kind
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
            issue__tracker_kind=self.tracker_kind,
            status=AgentRun.Status.SUCCEEDED,
        ).filter(Q(pull_request_url__gt="") | Q(phase="NoChangesRequired"))
        return {
            external_id async for external_id in rows.values_list("issue__external_id", flat=True)
        }

    async def safety_blocked_issue_ids(self) -> set[str]:
        from tempo_web.models import AgentRun

        rows = AgentRun.objects.filter(
            issue__tracker_kind=self.tracker_kind,
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

        issue, _ = await TrackedIssue.objects.aupdate_or_create(
            tracker_kind=self.tracker_kind,
            external_id=entry.issue.id,
            defaults=self._issue_defaults(entry.issue),
        )
        run = await AgentRun.objects.acreate(
            issue=issue,
            attempt=entry.attempt,
            phase=entry.phase,
            status=AgentRun.Status.RUNNING,
            workspace_path=str(workspace_path),
            started_at=entry.started_at,
        )
        await AgentSession.objects.acreate(run=run)
        return run.pk

    async def sync_issue(self, issue: Issue) -> None:
        from tempo_web.models import TrackedIssue

        await TrackedIssue.objects.aupdate_or_create(
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
        from tempo_web.models import AgentRun, ValidationAttempt

        await ValidationAttempt.objects.filter(
            run_id=entry.run_record_id,
            status=ValidationAttempt.Status.RUNNING,
        ).aupdate(
            status=ValidationAttempt.Status.INVALIDATED,
            finished_at=utcnow(),
        )

        await AgentRun.objects.filter(pk=entry.run_record_id).aupdate(
            phase=entry.phase,
            status=status,
            finished_at=utcnow(),
            error=error or "",
            pull_request_url=entry.session.pull_request_url or "",
            input_tokens=entry.session.codex_input_tokens,
            output_tokens=entry.session.codex_output_tokens,
            total_tokens=entry.session.codex_total_tokens,
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
