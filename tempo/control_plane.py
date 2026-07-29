from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from .orchestrator import Orchestrator


class ControlPlane:
    """Hosts independently scoped project orchestrators in one service process."""

    def __init__(self, workflow_paths: list[str]) -> None:
        if not workflow_paths:
            raise ValueError("at least one workflow path is required")
        self.orchestrators = [Orchestrator(path) for path in workflow_paths]
        self._subscriptions: dict[
            asyncio.Queue[None],
            list[tuple[Orchestrator, asyncio.Queue[None], asyncio.Task[None]]],
        ] = {}

    async def start(self) -> None:
        started: list[Orchestrator] = []
        try:
            for orchestrator in self.orchestrators:
                await orchestrator.start()
                started.append(orchestrator)
        except Exception:
            for orchestrator in reversed(started):
                await orchestrator.stop()
            raise
        project_keys = []
        for orchestrator in self.orchestrators:
            _, config = orchestrator.store.current()
            project_keys.append((config.project.organization, config.project.slug))
        if len(project_keys) != len(set(project_keys)):
            await self.stop()
            raise ValueError("each workflow must configure a unique project organization/slug")

    async def stop(self) -> None:
        await asyncio.gather(
            *(orchestrator.stop() for orchestrator in self.orchestrators),
            return_exceptions=True,
        )

    async def refresh(self) -> None:
        await asyncio.gather(*(orchestrator.refresh() for orchestrator in self.orchestrators))

    def snapshot(self) -> dict[str, Any]:
        snapshots = [orchestrator.snapshot() for orchestrator in self.orchestrators]
        if len(snapshots) == 1:
            result = snapshots[0]
            result["projects"] = [self._project_summary(self.orchestrators[0], snapshots[0])]
            return result
        totals: dict[str, int | float] = {}
        for snapshot in snapshots:
            for key, value in snapshot["totals"].items():
                if isinstance(value, (int, float)):
                    totals[key] = totals.get(key, 0) + value
        return {
            "service": {
                "project_count": len(snapshots),
                "tracker_kind": "multiple",
                "max_concurrent_agents": sum(
                    row["service"]["max_concurrent_agents"] for row in snapshots
                ),
                "last_tick_error": next(
                    (
                        row["service"]["last_tick_error"]
                        for row in snapshots
                        if row["service"]["last_tick_error"]
                    ),
                    None,
                ),
            },
            "projects": [
                self._project_summary(orchestrator, snapshot)
                for orchestrator, snapshot in zip(self.orchestrators, snapshots, strict=True)
            ],
            "running": [item for row in snapshots for item in row["running"]],
            "retries": [item for row in snapshots for item in row["retries"]],
            "claimed_count": sum(row["claimed_count"] for row in snapshots),
            "completed_count": sum(row["completed_count"] for row in snapshots),
            "safety_blocked_count": sum(row["safety_blocked_count"] for row in snapshots),
            "running_by_state": {},
            "totals": totals,
            "rate_limits": {
                self._project_key(orchestrator): snapshot["rate_limits"]
                for orchestrator, snapshot in zip(self.orchestrators, snapshots, strict=True)
            },
        }

    def admin_snapshot(self) -> dict[str, Any]:
        if len(self.orchestrators) == 1:
            result = self.orchestrators[0].admin_snapshot()
            result["projects"] = [result.copy()]
            return result
        projects = [orchestrator.admin_snapshot() for orchestrator in self.orchestrators]
        result = dict(projects[0])
        result["service"] = {
            **projects[0]["service"],
            **self.snapshot()["service"],
        }
        result["projects"] = projects
        result["runtime"] = {
            "running": sum(len(orchestrator.running) for orchestrator in self.orchestrators),
            "queued_retries": sum(len(orchestrator.retries) for orchestrator in self.orchestrators),
        }
        return result

    def issue_snapshot(self, identifier: str) -> dict[str, Any] | None:
        matches = [
            row
            for orchestrator in self.orchestrators
            if (row := orchestrator.issue_snapshot(identifier)) is not None
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    async def control_run(
        self,
        run_id: int,
        action: str,
        payload: dict[str, Any],
        *,
        user_id: int,
        idempotency_key: str,
    ) -> tuple[bool, str]:
        for orchestrator in self.orchestrators:
            if orchestrator.persistence and await orchestrator.persistence.run_control_context(
                run_id
            ):
                return await orchestrator.control_run(
                    run_id,
                    action,
                    payload,
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                )
        return False, "run_not_found"

    def subscribe_events(self) -> asyncio.Queue[None]:
        output: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        subscriptions = []
        for orchestrator in self.orchestrators:
            source = orchestrator.subscribe_events()
            task = asyncio.create_task(self._forward_events(source, output))
            subscriptions.append((orchestrator, source, task))
        self._subscriptions[output] = subscriptions
        return output

    def unsubscribe_events(self, queue: asyncio.Queue[None]) -> None:
        for orchestrator, source, task in self._subscriptions.pop(queue, []):
            orchestrator.unsubscribe_events(source)
            task.cancel()

    @staticmethod
    async def _forward_events(
        source: asyncio.Queue[None],
        output: asyncio.Queue[None],
    ) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await source.get()
                if output.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        output.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    output.put_nowait(None)

    @staticmethod
    def _project_key(orchestrator: Orchestrator) -> str:
        _, config = orchestrator.store.current()
        return f"{config.project.organization}/{config.project.slug}"

    @classmethod
    def _project_summary(
        cls,
        orchestrator: Orchestrator,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        _, config = orchestrator.store.current()
        return {
            "key": cls._project_key(orchestrator),
            "name": config.project.name,
            "environment": config.project.environment,
            "workflow_path": str(Path(orchestrator.store.path)),
            "tracker_kind": config.tracker.kind,
            "running": len(snapshot["running"]),
            "retries": len(snapshot["retries"]),
            "completed": snapshot["completed_count"],
        }
