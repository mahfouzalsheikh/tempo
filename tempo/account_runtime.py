"""Fail-closed account dispatch and durable session attribution."""

import re

from asgiref.sync import sync_to_async
from django.utils import timezone

from .account_binding import SELECTED_ACCOUNT
from .errors import ConfigError
from .run_snapshot import execution_setting
from .workload import EXECUTION_SCOPE


def begin_attempt(binding, scope, role, model, resume):
    from tempo_web.models import AgentAccountAttempt, AgentRun

    from .agent_accounts import resolve_binding

    account, data = resolve_binding(binding)
    match = re.fullmatch(r"run:(\d+):node:.+", scope)
    if not match:
        raise ValueError("Assigned accounts require a persisted workflow node.")
    run = AgentRun.objects.get(pk=int(match[1]), project_id=binding["project_id"])
    # Scope and pinned identity must agree before the provider receives a resume request.
    if (
        resume
        and not AgentAccountAttempt.objects.filter(
            run=run,
            scope=scope,
            binding=binding,
            thread_id=resume.thread_id,
        ).exists()
    ):
        raise ValueError("The saved session belongs to a different account assignment.")
    attempt = AgentAccountAttempt.objects.create(
        account=account,
        run=run,
        scope=scope,
        role=role,
        model=model or "",
        binding=binding,
    )
    return attempt, data


class AccountBoundRuntime:
    def __init__(self, runtime, binding, role):
        self.runtime = runtime
        self.binding = binding
        self.role = role
        self.selected_model = runtime.selected_model
        self.attempt = None

    async def finish(self, status):
        if self.attempt:
            from tempo_web.models import AgentAccountAttempt

            await AgentAccountAttempt.objects.filter(pk=self.attempt.pk).aupdate(
                status=status,
                finished_at=timezone.now(),
            )

    async def start_session(self, workspace, *, resume_context=None):
        if execution_setting("TEMPO_RUNTIME_BACKEND", "process") != "docker":
            raise ConfigError("Assigned accounts require isolated Docker runtimes.")
        try:
            self.attempt, data = await sync_to_async(begin_attempt)(
                self.binding,
                EXECUTION_SCOPE.get(),
                self.role,
                self.selected_model,
                resume_context,
            )
        except Exception as exc:
            raise ConfigError(
                "Assigned account preflight failed; check its login and project access."
            ) from exc
        token = SELECTED_ACCOUNT.set((self.binding, data))
        session = None
        try:
            session = await self.runtime.start_session(workspace, resume_context=resume_context)
            from tempo_web.models import AgentAccountAttempt

            await AgentAccountAttempt.objects.filter(pk=self.attempt.pk).aupdate(
                status="active",
                thread_id=session.thread_id,
            )
            return session
        except BaseException:
            try:
                if session is not None:
                    await self.runtime.stop_session(session)
            finally:
                await self.finish("failed")
            raise
        finally:
            SELECTED_ACCOUNT.reset(token)

    async def run_turn(self, session, prompt, issue):
        from .agent_accounts import resolve_binding

        try:
            await sync_to_async(resolve_binding)(self.binding)
        except Exception as exc:
            await self.finish("blocked")
            raise ConfigError("Assigned account access was revoked or its login changed.") from exc
        try:
            return await self.runtime.run_turn(session, prompt, issue)
        except BaseException:
            await self.finish("failed")
            raise

    async def stop_session(self, session):
        try:
            await self.runtime.stop_session(session)
        except BaseException:
            await self.finish("cleanup_failed")
            raise
        else:
            if self.attempt:
                from tempo_web.models import AgentAccountAttempt

                await AgentAccountAttempt.objects.filter(
                    pk=self.attempt.pk, status="active"
                ).aupdate(
                    status="stopped",
                    finished_at=timezone.now(),
                )
