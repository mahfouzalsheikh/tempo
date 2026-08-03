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
    model: str | None = None

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


class RuntimeProviderConfig(BaseModel):
    """Declarative agent-runtime endpoint. Runtime implementations are registry-backed."""

    kind: str = "codex"
    command: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_nonblank(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("must not be blank")
        return value


class ModelRouteConfig(BaseModel):
    model: str
    fallbacks: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    max_cost_per_million_tokens: float | None = Field(default=None, gt=0)

    @field_validator("model")
    @classmethod
    def model_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ModelProviderConfig(BaseModel):
    """Model-provider and routing metadata consumed by an AgentRuntime."""

    kind: str = "openai"
    model: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    routes: list[ModelRouteConfig] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_nonblank(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("must not be blank")
        return value


class ToolProviderConfig(BaseModel):
    """A named tool bundle. Empty ``tools`` means every native Tempo tool."""

    kind: str = "tempo"
    allow_all: bool = True
    tools: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_nonblank(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("tools", mode="before")
    @classmethod
    def tools_normalized(cls, value: list[str] | None) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in (value or []) if str(item).strip()))


class AgentProfileConfig(BaseModel):
    """A specialist role and its runtime/model/tool policy."""

    role: str = "implementer"
    runtime: str = "codex"
    model: str = "default"
    tool_providers: list[str] = Field(default_factory=lambda: ["tempo"])
    prompt: str = ""
    max_turns: int | None = Field(default=None, gt=0)
    completion: Literal["turn", "validation", "publication"] = "publication"
    capabilities: list[str] = Field(default_factory=list)
    max_model_cost_per_million_tokens: float | None = Field(default=None, gt=0)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("role", "runtime", "model")
    @classmethod
    def reference_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class WorkflowNodeConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    type: Literal["agent", "human_gate", "join"] = "agent"
    name: str | None = None
    agent: str | None = None
    prompt: str = ""
    max_retries: int = Field(default=0, ge=0, le=20)
    approval_message: str = "Approve this workflow gate to continue."
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def id_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def agent_required(self) -> WorkflowNodeConfig:
        if self.type == "agent" and not self.agent:
            raise ValueError("agent nodes must reference an agent profile")
        if self.type != "agent" and self.agent:
            raise ValueError("only agent nodes may reference an agent profile")
        return self


class WorkflowEdgeConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    condition: str = "succeeded"

    @field_validator("source", "target", "condition")
    @classmethod
    def value_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class WorkflowGraphConfig(BaseModel):
    name: str = "issue-to-pull-request"
    max_parallel_nodes: int = Field(default=1, gt=0, le=50)
    require_publication: bool = True
    nodes: list[WorkflowNodeConfig] = Field(
        default_factory=lambda: [
            WorkflowNodeConfig(id="implementation", agent="implementer")
        ]
    )
    edges: list[WorkflowEdgeConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_dag(self) -> WorkflowGraphConfig:
        node_ids = [node.id for node in self.nodes]
        if not node_ids:
            raise ValueError("workflow graph must contain at least one node")
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workflow graph node ids must be unique")
        known = set(node_ids)
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValueError(
                    f"workflow edge {edge.source}->{edge.target} references an unknown node"
                )
            if edge.source == edge.target:
                raise ValueError("workflow graph nodes cannot depend on themselves")
            adjacency[edge.source].append(edge.target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("workflow graph must be acyclic")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in adjacency[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_ids:
            visit(node_id)
        return self


class ServiceConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    tracker: TrackerConfig
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    runtime_providers: dict[str, RuntimeProviderConfig] = Field(
        default_factory=lambda: {"codex": RuntimeProviderConfig()}
    )
    model_providers: dict[str, ModelProviderConfig] = Field(
        default_factory=lambda: {"default": ModelProviderConfig()}
    )
    tool_providers: dict[str, ToolProviderConfig] = Field(
        default_factory=lambda: {"tempo": ToolProviderConfig()}
    )
    agents: dict[str, AgentProfileConfig] = Field(
        default_factory=lambda: {"implementer": AgentProfileConfig()}
    )
    workflow: WorkflowGraphConfig = Field(default_factory=WorkflowGraphConfig)

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
        if not self.runtime_providers:
            raise ValueError("at least one runtime provider is required")
        if not self.model_providers:
            raise ValueError("at least one model provider is required")
        for name, profile in self.agents.items():
            if not name.strip():
                raise ValueError("agent profile names must not be blank")
            if profile.runtime not in self.runtime_providers:
                raise ValueError(
                    f"agent {name} references unknown runtime provider {profile.runtime}"
                )
            if profile.model not in self.model_providers:
                raise ValueError(f"agent {name} references unknown model provider {profile.model}")
            missing_tools = set(profile.tool_providers) - set(self.tool_providers)
            if missing_tools:
                raise ValueError(
                    f"agent {name} references unknown tool providers: {sorted(missing_tools)}"
                )
        for node in self.workflow.nodes:
            if node.type == "agent" and node.agent not in self.agents:
                raise ValueError(
                    f"workflow node {node.id} references unknown agent profile {node.agent}"
                )
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
    tracker["provider"] = provider
    payload["tracker"] = tracker

    try:
        return ServiceConfig.model_validate(payload)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
