from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_state(value: str) -> str:
    return value.strip().lower()


class BlockerRef(BaseModel):
    id: str | None = None
    identifier: str | None = None
    state: str | None = None


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    native_ref: dict[str, Any] | None = None
    identifier: str
    title: str
    description: str | None = None
    priority: int | None = None
    state: str
    branch_name: str | None = None
    url: str | None = None
    assignee_id: str | None = None
    labels: list[str] = Field(default_factory=list)
    blocked_by: list[BlockerRef] = Field(default_factory=list)
    dispatchable: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("id", "identifier", "title", "state")
    @classmethod
    def required_nonblank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: list[str] | None) -> list[str]:
        return sorted({str(item).strip().lower() for item in (value or []) if str(item).strip()})


@dataclass(frozen=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str
    path: Path
    mtime_ns: int


@dataclass(frozen=True)
class Workspace:
    path: Path
    workspace_key: str
    created_now: bool


@dataclass
class LiveSession:
    agent_role: str = "implementation"
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    codex_app_server_pid: str | None = None
    last_codex_event: str | None = None
    last_codex_timestamp: datetime | None = None
    last_codex_message: dict[str, Any] | None = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    thread_input_tokens: int = 0
    thread_output_tokens: int = 0
    thread_total_tokens: int = 0
    turn_count: int = 0
    validation_status: str = "pending"
    validation_summary: str | None = None
    validation_started_at: datetime | None = None
    validation_finished_at: datetime | None = None
    current_validation_command: str | None = None
    validation_commands: list[dict[str, Any]] = field(default_factory=list)
    validation_attempt_count: int = 0
    successful_validation_count: int = 0
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    pull_request_created: bool = False
    pull_request_url: str | None = None
    pull_request_number: int | None = None
    review_status: str = "pending"
    review_summary: str | None = None
    human_review_reason: str | None = None
    merged: bool = False
    no_change_completed: bool = False
    completion_summary: str | None = None
    validation_record_id: int | None = None
    active_node_id: str | None = None
    active_agent_role: str | None = None


@dataclass
class NodeExecutionState:
    node_id: str
    name: str
    node_type: str
    agent: str | None = None
    role: str | None = None
    runtime: str | None = None
    model: str | None = None
    status: str = "pending"
    attempt: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunningEntry:
    issue: Issue
    task: Any
    attempt: int | None
    started_at: datetime = field(default_factory=utcnow)
    phase: str = "PreparingWorkspace"
    session: LiveSession = field(default_factory=LiveSession)
    run_record_id: int | None = None
    graph_nodes: dict[str, NodeExecutionState] = field(default_factory=dict)
    node_sessions: dict[str, LiveSession] = field(default_factory=dict)
    node_sessions_aggregated: bool = False


@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at: datetime
    error: str | None = None
    run_record_id: int | None = None


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    runtime_seconds: float = 0.0
    validation_passes: int = 0
    validation_failures: int = 0
    validated_runs: int = 0
