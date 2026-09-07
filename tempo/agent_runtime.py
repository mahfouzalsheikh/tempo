from __future__ import annotations

import asyncio
import contextlib
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex import CodexAppServer
from .config import (
    AgentProfileConfig,
    ModelProviderConfig,
    RuntimeProviderConfig,
    ServiceConfig,
    ToolProviderConfig,
)
from .credentials import CONTROL_PLANE_SECRETS, process_environment
from .domain import Issue
from .errors import CodexError, ConfigError
from .process import stop_process_group
from .trackers.base import Tracker
from .workspace import WorkspaceManager

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str | None
    candidates: tuple[str, ...]
    settings: dict[str, Any]


@dataclass(frozen=True)
class RuntimeResumeContext:
    """Durable provider session state used to continue an interrupted node."""

    thread_id: str
    usage_baseline: dict[str, int]
    compact_before_resume: bool = False
    workspace_published: bool = False


class ModelProvider(ABC):
    @abstractmethod
    def resolve(
        self,
        config: ModelProviderConfig,
        profile: AgentProfileConfig,
        *,
        candidate_index: int = 0,
    ) -> ModelSelection:
        """Resolve a declarative model route for a runtime."""


class ConfiguredModelProvider(ModelProvider):
    def resolve(
        self,
        config: ModelProviderConfig,
        profile: AgentProfileConfig,
        *,
        candidate_index: int = 0,
    ) -> ModelSelection:
        requested_capabilities = set(profile.capabilities)
        route = next(
            (
                item
                for item in config.routes
                if (not item.roles or profile.role in item.roles)
                and requested_capabilities.issubset(set(item.capabilities))
                and (
                    profile.max_model_cost_per_million_tokens is None
                    or item.max_cost_per_million_tokens is None
                    or item.max_cost_per_million_tokens
                    <= profile.max_model_cost_per_million_tokens
                )
            ),
            None,
        )
        candidates = tuple(
            dict.fromkeys(
                ([route.model, *route.fallbacks] if route else [])
                + ([config.model] if config.model else [])
                + config.fallbacks
            )
        )
        selected = candidates[min(candidate_index, len(candidates) - 1)] if candidates else None
        return ModelSelection(config.kind, selected, candidates, dict(config.settings))


class ToolProvider(ABC):
    @abstractmethod
    def resolve(self, config: ToolProviderConfig) -> set[str] | None:
        """Return enabled tool names, or ``None`` for every native tool."""


class TempoToolProvider(ToolProvider):
    def resolve(self, config: ToolProviderConfig) -> set[str] | None:
        return None if config.allow_all else set(config.tools)


class AgentRuntime(ABC):
    """Provider-neutral lifecycle used by the workflow graph executor."""

    @abstractmethod
    async def start_session(
        self,
        workspace: Path,
        *,
        resume_context: RuntimeResumeContext | None = None,
    ) -> Any:
        pass

    @abstractmethod
    async def run_turn(self, session: Any, prompt: str, issue: Issue) -> dict[str, Any]:
        pass

    @abstractmethod
    async def stop_session(self, session: Any) -> None:
        pass


class CodexAgentRuntime(AgentRuntime):
    def __init__(
        self,
        service_config: ServiceConfig,
        runtime_config: RuntimeProviderConfig,
        model: ModelSelection,
        enabled_tools: set[str] | None,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
        on_event: EventCallback,
        approval_callback: ApprovalCallback | None,
    ) -> None:
        self.selected_model = model.model
        allowed_overrides = {
            "approval_policy",
            "thread_sandbox",
            "turn_sandbox_policy",
            "turn_timeout_ms",
            "read_timeout_ms",
            "stall_timeout_ms",
        }
        overrides = {
            key: value
            for key, value in runtime_config.settings.items()
            if key in allowed_overrides
        }
        if runtime_config.command:
            overrides["command"] = runtime_config.command
        if model.model:
            overrides["model"] = model.model
        overrides["environment"] = {
            **service_config.codex.environment, **runtime_config.environment,
        }
        codex_config = service_config.codex.model_copy(update=overrides)
        resolved_config = service_config.model_copy(update={"codex": codex_config})
        self.client = CodexAppServer(
            resolved_config,
            workspace_manager,
            tracker,
            on_event,
            approval_callback=approval_callback,
            enabled_tools=enabled_tools,
        )

    async def start_session(
        self,
        workspace: Path,
        *,
        resume_context: RuntimeResumeContext | None = None,
    ) -> Any:
        session = await self.client.start_session(
            workspace,
            resume_thread_id=(resume_context.thread_id if resume_context else None),
            usage_baseline=(resume_context.usage_baseline if resume_context else None),
        )
        if resume_context and resume_context.compact_before_resume and session.resumed:
            try:
                await self.client.compact_session(session)
            except BaseException:
                await self.client.stop_session(session)
                raise
        return session

    async def run_turn(self, session: Any, prompt: str, issue: Issue) -> dict[str, Any]:
        return await self.client.run_turn(session, prompt, issue)

    async def stop_session(self, session: Any) -> None:
        await self.client.stop_session(session)


@dataclass
class ExternalCommandSession:
    process: asyncio.subprocess.Process
    workspace: Path
    next_request_id: int = 2
    resumed: bool = False
    resume_failure: str | None = None


class ExternalCommandRuntime(AgentRuntime):
    """JSONL bridge for OpenAI Agents SDK services and other external runtimes."""

    def __init__(
        self,
        service_config: ServiceConfig,
        runtime_config: RuntimeProviderConfig,
        model: ModelSelection,
        enabled_tools: set[str] | None,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
        on_event: EventCallback,
        approval_callback: ApprovalCallback | None,
    ) -> None:
        if not runtime_config.command:
            raise ConfigError(
                f"runtime {runtime_config.kind} requires a JSONL bridge command"
            )
        self.command = runtime_config.command
        self.environment = dict(runtime_config.environment)
        self.runner_secret_name = service_config.validation.runner_token[1:]
        self.settings = dict(runtime_config.settings)
        self.model = model
        self.selected_model = model.model
        self.enabled_tools = enabled_tools
        self.workspace_manager = workspace_manager
        self.tracker = tracker
        self.on_event = on_event
        self.approval_callback = approval_callback
        self.timeout_ms = int(self.settings.get("turn_timeout_ms", 3_600_000))

    async def start_session(
        self,
        workspace: Path,
        *,
        resume_context: RuntimeResumeContext | None = None,
    ) -> ExternalCommandSession:
        self.workspace_manager.assert_contained(workspace)
        await self.tracker.assert_ownership()
        environment = process_environment(
            self.environment,
            forbidden=CONTROL_PLANE_SECRETS | self.tracker.secret_environment_names()
            | {self.runner_secret_name},
        )
        process = await asyncio.create_subprocess_exec(
            "bash",
            "--noprofile", "--norc", "-c",
            f"exec {self.command}",
            cwd=workspace,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        session = ExternalCommandSession(process=process, workspace=workspace)
        asyncio.create_task(self._drain_stderr(process))
        try:
            result = await self._request(
                session,
                1,
                "session/start",
                {
                    "workspace": str(workspace),
                    "model_provider": self.model.provider,
                    "model": self.model.model,
                    "model_candidates": list(self.model.candidates),
                    "tools": sorted(self.enabled_tools) if self.enabled_tools is not None else None,
                    "settings": self.settings,
                    "resume": (
                        {
                            "thread_id": resume_context.thread_id,
                            "usage_baseline": resume_context.usage_baseline,
                            "compact_before_resume": resume_context.compact_before_resume,
                        }
                        if resume_context
                        else None
                    ),
                },
            )
            session.resumed = bool(result.get("resumed"))
            if resume_context and not session.resumed:
                session.resume_failure = str(
                    result.get("resume_failure") or "runtime did not confirm session continuation"
                )
        except BaseException:
            await stop_process_group(process)
            raise
        return session

    async def run_turn(
        self,
        session: ExternalCommandSession,
        prompt: str,
        issue: Issue,
    ) -> dict[str, Any]:
        await self.tracker.assert_ownership()
        request_id = session.next_request_id
        session.next_request_id += 1
        result = await self._request(
            session,
            request_id,
            "turn/run",
            {
                "prompt": prompt,
                "issue": issue.model_dump(mode="json"),
                "workspace": str(session.workspace),
            },
        )
        await self.on_event(
            {
                "event": "turn/completed",
                "payload": result,
            }
        )
        return result

    async def stop_session(self, session: ExternalCommandSession) -> None:
        try:
            if session.process.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self._request(
                            session, session.next_request_id, "session/stop", {}, timeout_ms=3000,
                        ),
                        timeout=3,
                    )
        finally:
            await stop_process_group(session.process)

    async def _request(
        self,
        session: ExternalCommandSession,
        request_id: int,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        if not session.process.stdin or not session.process.stdout:
            raise CodexError("external runtime pipes are unavailable", category="port_exit")
        message = {"id": request_id, "method": method, "params": params}
        session.process.stdin.write((json.dumps(message, default=str) + "\n").encode())
        await session.process.stdin.drain()
        timeout = (timeout_ms or self.timeout_ms) / 1000
        while True:
            raw = await asyncio.wait_for(session.process.stdout.readline(), timeout=timeout)
            if not raw:
                raise CodexError("external runtime exited", category="port_exit")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CodexError(
                    "external runtime returned invalid JSONL", category="response_error"
                ) from exc
            if response.get("id") == request_id:
                if response.get("error"):
                    raise CodexError(
                        str(response["error"]), category="external_runtime_error"
                    )
                result = response.get("result", {})
                return result if isinstance(result, dict) else {"result": result}
            event = response if "event" in response else {
                "event": str(response.get("method", "external/event")),
                "payload": response.get("params", response),
            }
            await self.on_event(event)

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
        if process.stderr:
            while await process.stderr.readline():
                pass


RuntimeFactory = Callable[..., AgentRuntime]


class ProviderRegistry:
    """Extension boundary for runtimes, model providers, and tool providers."""

    def __init__(self) -> None:
        self.runtime_factories: dict[str, RuntimeFactory] = {
            "codex": CodexAgentRuntime,
            "external": ExternalCommandRuntime,
            "openai-agents": ExternalCommandRuntime,
        }
        self.model_factories: dict[str, type[ModelProvider]] = {
            "openai": ConfiguredModelProvider,
            "static": ConfiguredModelProvider,
        }
        self.tool_factories: dict[str, type[ToolProvider]] = {"tempo": TempoToolProvider}

    def register_runtime(self, kind: str, factory: RuntimeFactory) -> None:
        self.runtime_factories[kind.strip().lower()] = factory

    def register_model_provider(self, kind: str, provider: type[ModelProvider]) -> None:
        self.model_factories[kind.strip().lower()] = provider

    def register_tool_provider(self, kind: str, provider: type[ToolProvider]) -> None:
        self.tool_factories[kind.strip().lower()] = provider

    def create_runtime(
        self,
        config: ServiceConfig,
        profile: AgentProfileConfig,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
        on_event: EventCallback,
        approval_callback: ApprovalCallback | None,
        model_index: int = 0,
    ) -> AgentRuntime:
        runtime_config = config.runtime_providers[profile.runtime]
        runtime_factory = self.runtime_factories.get(runtime_config.kind)
        if not runtime_factory:
            raise ConfigError(f"unsupported agent runtime kind: {runtime_config.kind}")
        model_config = config.model_providers[profile.model]
        model_factory = self.model_factories.get(model_config.kind)
        if not model_factory:
            raise ConfigError(f"unsupported model provider kind: {model_config.kind}")
        model = model_factory().resolve(model_config, profile, candidate_index=model_index)
        enabled_tools = self.enabled_tools(config, profile)
        return runtime_factory(
            config,
            runtime_config,
            model,
            enabled_tools,
            workspace_manager,
            tracker,
            on_event,
            approval_callback,
        )

    def enabled_tools(
        self,
        config: ServiceConfig,
        profile: AgentProfileConfig,
    ) -> set[str] | None:
        """Resolve the tools visible to an agent profile."""
        enabled_tools: set[str] | None = set()
        for provider_name in profile.tool_providers:
            tool_config = config.tool_providers[provider_name]
            tool_factory = self.tool_factories.get(tool_config.kind)
            if not tool_factory:
                raise ConfigError(f"unsupported tool provider kind: {tool_config.kind}")
            resolved = tool_factory().resolve(tool_config)
            if resolved is None:
                return None
            enabled_tools.update(resolved)
        return enabled_tools

    def model_candidates(
        self,
        config: ServiceConfig,
        profile: AgentProfileConfig,
    ) -> tuple[str, ...]:
        model_config = config.model_providers[profile.model]
        model_factory = self.model_factories.get(model_config.kind)
        if not model_factory:
            raise ConfigError(f"unsupported model provider kind: {model_config.kind}")
        return model_factory().resolve(model_config, profile).candidates


providers = ProviderRegistry()
