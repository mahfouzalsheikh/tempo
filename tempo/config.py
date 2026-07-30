from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import normalize_state
from .errors import ConfigError


class TrackerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    provider: dict[str, Any] = Field(default_factory=dict)
    required_labels: list[str] = Field(default_factory=list)
    active_states: list[str]
    terminal_states: list[str]

    @field_validator("kind")
    @classmethod
    def kind_supported(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"github", "memory"}:
            raise ValueError("supported tracker kinds are: github, memory")
        return value

    @field_validator("required_labels", mode="before")
    @classmethod
    def labels_normalized(cls, value: list[str] | None) -> list[str]:
        return list(dict.fromkeys(str(v).strip().lower() for v in (value or [])))

    @field_validator("active_states", "terminal_states")
    @classmethod
    def states_nonempty(cls, value: list[str]) -> list[str]:
        values = [str(item).strip() for item in value if str(item).strip()]
        if not values:
            raise ValueError("must contain at least one state")
        return values


class PollingConfig(BaseModel):
    interval_ms: int = Field(default=30_000, gt=0)


class WorkspaceConfig(BaseModel):
    root: Path


class HooksConfig(BaseModel):
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = Field(default=60_000, gt=0)


class AgentConfig(BaseModel):
    max_concurrent_agents: int = Field(default=10, gt=0)
    max_turns: int = Field(default=20, gt=0)
    max_tokens_per_run: int = Field(default=1_000_000, gt=0)
    max_retries: int = Field(default=2, ge=0, le=20)
    max_retry_backoff_ms: int = Field(default=300_000, gt=0)
    max_concurrent_agents_by_state: dict[str, int] = Field(default_factory=dict)

    @field_validator("max_concurrent_agents_by_state", mode="before")
    @classmethod
    def valid_state_limits(cls, value: dict[str, Any] | None) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, limit in (value or {}).items():
            if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                result[normalize_state(str(key))] = limit
        return result


class ValidationConfig(BaseModel):
    enabled: bool = True
    runner_url: str | None = None
    command_timeout_ms: int = Field(default=1_800_000, gt=0)
    cleanup_timeout_ms: int = Field(default=120_000, gt=0)
    max_commands: int = Field(default=12, gt=0, le=50)
    max_attempts_per_run: int = Field(default=5, gt=0, le=50)
    max_output_chars: int = Field(default=40_000, gt=0)


class ReviewConfig(BaseModel):
    enabled: bool = False
    max_turns: int = Field(default=3, gt=0, le=20)
    auto_merge: bool = False
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    reviewers: list[str] = Field(default_factory=list)
    team_reviewers: list[str] = Field(default_factory=list)
    prompt: str = (
        "Act as an independent reviewer. Inspect the issue, pull request, complete diff, and "
        "repository guidance. Look for correctness, regressions, security problems, missing tests, "
        "and maintainability issues. Fix material findings when safe, rerun project validation, "
        "and update the pull-request branch. Approve only when the reviewed workspace is "
        "validated. If policy, ambiguity, permissions, sensitive changes, or unresolved risk "
        "require a person, request human review with a precise reason."
    )

    @field_validator("reviewers", "team_reviewers", mode="before")
    @classmethod
    def normalized_reviewers(cls, value: list[str] | None) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in (value or []) if str(item).strip()))

    @field_validator("prompt")
    @classmethod
    def prompt_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class CodexConfig(BaseModel):
    command: str = "codex app-server"
    approval_policy: str | dict[str, Any] = Field(
        default_factory=lambda: {
            "reject": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": True,
            }
        }
    )
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: dict[str, Any] | None = None
    turn_timeout_ms: int = Field(default=3_600_000, gt=0)
    read_timeout_ms: int = Field(default=5_000, gt=0)
    stall_timeout_ms: int = Field(default=300_000, ge=0)

    @field_validator("command")
    @classmethod
    def command_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ProjectConfig(BaseModel):
    organization: str = "default"
    slug: str = "default"
    name: str = "Default project"
    environment: str = "development"
    max_concurrent_runs: int = Field(default=3, gt=0)
    environment_max_concurrent_runs: int = Field(default=3, gt=0)

    @field_validator("organization", "slug", "environment")
    @classmethod
    def identifier_nonblank(cls, value: str) -> str:
        normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class ServiceConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    tracker: TrackerConfig
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)

    @model_validator(mode="after")
    def disjoint_states(self) -> ServiceConfig:
        active = {normalize_state(v) for v in self.tracker.active_states}
        terminal = {normalize_state(v) for v in self.tracker.terminal_states}
        if active & terminal:
            raise ValueError("active_states and terminal_states must not overlap")
        if self.tracker.kind == "github":
            repo = str(self.tracker.provider.get("repo", "")).strip()
            if len(repo.split("/")) != 2 or any(not part for part in repo.split("/")):
                raise ValueError("tracker.provider.repo must be owner/repo for GitHub")
        return self

    @property
    def active_states(self) -> set[str]:
        return {normalize_state(v) for v in self.tracker.active_states}

    @property
    def terminal_states(self) -> set[str]:
        return {normalize_state(v) for v in self.tracker.terminal_states}


def resolve_env_reference(value: Any, *, empty_is_error: bool = False) -> Any:
    if isinstance(value, str) and value.startswith("$") and value[1:].replace("_", "").isalnum():
        result = os.environ.get(value[1:], "")
        if empty_is_error and not result:
            raise ConfigError(f"environment variable {value} is not set")
        return result
    return value


def build_config(raw: dict[str, Any], workflow_path: Path) -> ServiceConfig:
    payload = dict(raw)
    workspace = dict(payload.get("workspace") or {})
    root_value = workspace.get("root", Path(tempfile.gettempdir()) / "tempo_workspaces")
    root_value = resolve_env_reference(root_value, empty_is_error=True)
    root = Path(str(root_value)).expanduser()
    if not root.is_absolute():
        root = workflow_path.parent / root
    workspace["root"] = root.resolve(strict=False)
    payload["workspace"] = workspace

    tracker = dict(payload.get("tracker") or {})
    provider = dict(tracker.get("provider") or {})
    for secret_key in ("token", "api_key"):
        if secret_key in provider:
            provider[secret_key] = resolve_env_reference(provider[secret_key], empty_is_error=True)
    if "review_token" in provider:
        provider["review_token"] = resolve_env_reference(
            provider["review_token"],
            empty_is_error=False,
        )
    tracker["provider"] = provider
    payload["tracker"] = tracker

    try:
        return ServiceConfig.model_validate(payload)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
