"""Versioned execution inputs, captured before dispatch and verified before reuse."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tomllib
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

from .account_binding import has_bindings
from .errors import ConfigError

PINNED_EXECUTION = ContextVar("pinned_execution", default=None)
EXECUTION_DEFAULTS = {
    "TEMPO_RUNTIME_BACKEND": "process",
    "TEMPO_RUNTIME_IMAGE": None,
    "TEMPO_RUNTIME_TIMEOUT_MS": "28800000",
    "TEMPO_AGENT_STATE_ROOT": "/data/agent-state",
    "TEMPO_VALIDATION_BACKEND": "docker",
    "TEMPO_VALIDATION_IMAGE": None,
    "TEMPO_VALIDATION_RUNNER_URL": None,
}
MODEL_SETTINGS = (
    "model", "model_reasoning_effort", "personality", "service_tier",
    "forced_login_method", "forced_chatgpt_workspace_id",
)


def execution_setting(name: str, default=None):
    pinned = PINNED_EXECUTION.get()
    if pinned is not None:
        value = pinned["environment"].get(name)
        return value if value is not None else default
    return os.getenv(name, default)


def portable_model_settings() -> dict:
    pinned = PINNED_EXECUTION.get()
    if pinned is not None:
        return deepcopy(pinned["model_settings"])
    source = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    settings = tomllib.loads(source.read_text()) if source.exists() else {}
    return {key: settings[key] for key in MODEL_SETTINGS if isinstance(settings.get(key), str)}


@contextlib.contextmanager
def execution_settings(execution: dict):
    token = PINNED_EXECUTION.set(deepcopy(execution))
    try:
        yield
    finally:
        PINNED_EXECUTION.reset(token)


def snapshot_digest(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def capture_snapshot(definition, config) -> dict:
    # Resolve only nonsecret execution defaults. Credentials remain declarative references.
    return {
        "schema": 2 if has_bindings(config) else 1,
        "config": config.model_dump(mode="json"),
        "prompt_template": definition.prompt_template,
        "workflow_path": str(definition.path),
        "execution": {
            "environment": {
                key: os.getenv(key, value) for key, value in EXECUTION_DEFAULTS.items()
            },
            "model_settings": portable_model_settings(),
        },
    }


def restore_snapshot(snapshot: dict, digest: str):
    from .config import ServiceConfig
    from .domain import WorkflowDefinition

    if not snapshot or not digest:
        raise ConfigError(
            "This run predates complete execution snapshots. Its original prompt and execution "
            "defaults cannot be recovered safely; it cannot resume using current configuration.",
            category="snapshot_missing",
        )
    try:
        if set(snapshot) != {"schema", "config", "prompt_template", "workflow_path", "execution"}:
            raise ValueError("unsupported snapshot shape")
        if (snapshot_digest(snapshot) != digest or type(snapshot["schema"]) is not int
                or snapshot["schema"] not in {1, 2}):
            raise ValueError("snapshot identity mismatch")
        if set(snapshot["execution"]) != {"environment", "model_settings"}:
            raise ValueError("unsupported execution settings")
        if set(snapshot["execution"]["environment"]) != set(EXECUTION_DEFAULTS):
            raise ValueError("incomplete execution defaults")
        if any(value is not None and not isinstance(value, str)
               for value in snapshot["execution"]["environment"].values()):
            raise ValueError("invalid execution defaults")
        model_settings = snapshot["execution"]["model_settings"]
        if set(model_settings) - set(MODEL_SETTINGS) or any(
            not isinstance(value, str) for value in model_settings.values()
        ):
            raise ValueError("invalid model settings")
        if not isinstance(snapshot["prompt_template"], str):
            raise ValueError("invalid prompt")
        config = ServiceConfig.model_validate(deepcopy(snapshot["config"]))
        if snapshot["schema"] == 1 and has_bindings(config):
            raise ValueError("Account assignments require snapshot schema 2")
        if config.model_dump(mode="json") != snapshot["config"]:
            raise ValueError("configuration schema requires an explicit migration")
        definition = WorkflowDefinition(
            config=deepcopy(snapshot["config"]), prompt_template=snapshot["prompt_template"],
            path=Path(snapshot["workflow_path"]), mtime_ns=0,
        )
        return definition, config
    except Exception as exc:
        raise ConfigError(
            "The saved execution snapshot is invalid or unsupported; execution is blocked.",
            category="snapshot_invalid",
        ) from exc


def check_execution_host(snapshot: dict) -> None:
    for name in ("TEMPO_RUNTIME_BACKEND", "TEMPO_VALIDATION_BACKEND"):
        if snapshot["execution"]["environment"][name] != os.getenv(name, EXECUTION_DEFAULTS[name]):
            raise ConfigError(
                "The run's saved execution backend differs from this installation. "
                "Restore compatible execution infrastructure before retrying.",
                category="snapshot_environment_changed",
            )
