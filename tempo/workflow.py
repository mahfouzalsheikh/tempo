from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from .config import ServiceConfig, build_config
from .domain import Issue, WorkflowDefinition
from .errors import ConfigError, WorkflowError

DEFAULT_PROMPT = "You are working on an issue from the configured tracker."
PLATFORM_SECTION_NAMES = frozenset(
    {
        "runtime_providers",
        "model_providers",
        "tool_providers",
        "agents",
        "workflow",
    }
)


def load_workflow(path: str | Path) -> WorkflowDefinition:
    workflow_path = Path(path).expanduser().resolve(strict=False)
    try:
        text = workflow_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(
            f"cannot read workflow file {workflow_path}: {exc}",
            category="missing_workflow_file",
        ) from exc

    config: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        try:
            closing = lines[1:].index("---") + 1
        except ValueError as exc:
            raise WorkflowError("YAML front matter is missing its closing ---") from exc
        try:
            parsed = yaml.safe_load("\n".join(lines[1:closing]))
        except yaml.YAMLError as exc:
            raise WorkflowError(f"invalid YAML front matter: {exc}") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise WorkflowError("YAML front matter must decode to an object")
        config = parsed or {}
        body = "\n".join(lines[closing + 1 :])

    return WorkflowDefinition(
        config=config,
        prompt_template=body.strip(),
        path=workflow_path,
        mtime_ns=workflow_path.stat().st_mtime_ns,
    )


def render_prompt(definition: WorkflowDefinition, issue: Issue, attempt: int | None) -> str:
    environment = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = environment.from_string(definition.prompt_template or DEFAULT_PROMPT)
        return template.render(issue=issue.model_dump(mode="json"), attempt=attempt).strip()
    except TemplateError as exc:
        raise WorkflowError(
            f"prompt rendering failed: {exc}", category="prompt_render_error"
        ) from exc


def render_node_prompt(
    template: str,
    *,
    issue: Issue,
    attempt: int | None,
    node: dict[str, Any],
    graph: dict[str, Any],
) -> str:
    environment = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        return environment.from_string(template).render(
            issue=issue.model_dump(mode="json"),
            attempt=attempt,
            node=node,
            graph=graph,
        ).strip()
    except TemplateError as exc:
        raise WorkflowError(
            f"node prompt rendering failed: {exc}", category="prompt_render_error"
        ) from exc


class WorkflowStore:
    """Keeps the last known-good dynamically reloadable workflow."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.definition: WorkflowDefinition | None = None
        self.config: ServiceConfig | None = None
        self.last_error: str | None = None
        self.managed_sections: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> tuple[WorkflowDefinition, ServiceConfig]:
        async with self._lock:
            definition = load_workflow(self.path)
            config = build_config(self._effective_config(definition.config), definition.path)
            self.definition, self.config, self.last_error = definition, config, None
            return definition, config

    async def reload_if_changed(self) -> bool:
        async with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
                if self.definition and self.definition.mtime_ns == mtime:
                    return False
                definition = load_workflow(self.path)
                config = build_config(self._effective_config(definition.config), definition.path)
            except Exception as exc:
                self.last_error = str(exc)
                return False
            self.definition, self.config, self.last_error = definition, config, None
            return True

    def current(self) -> tuple[WorkflowDefinition, ServiceConfig]:
        if not self.definition or not self.config:
            raise RuntimeError("workflow store is not initialized")
        return self.definition, self.config

    async def update_platform_sections(self, sections: dict[str, Any]) -> ServiceConfig:
        """Validate and merge operator-managed sections over the file definition."""

        self._validate_platform_sections(sections)
        async with self._lock:
            if not self.definition:
                raise RuntimeError("workflow store is not initialized")
            merged = {**self.managed_sections, **deepcopy(sections)}
            raw = deepcopy(self.definition.config)
            raw.update(merged)
            config = build_config(raw, self.definition.path)
            self.managed_sections = merged
            self.config = config
            self.last_error = None
            return config

    async def replace_platform_sections(self, sections: dict[str, Any]) -> ServiceConfig:
        """Replace the database-managed sections and rebuild the effective config."""

        self._validate_platform_sections(sections)
        async with self._lock:
            if not self.definition:
                raise RuntimeError("workflow store is not initialized")
            raw = deepcopy(self.definition.config)
            raw.update(deepcopy(sections))
            config = build_config(raw, self.definition.path)
            self.managed_sections = deepcopy(sections)
            self.config = config
            self.last_error = None
            return config

    @staticmethod
    def _validate_platform_sections(sections: dict[str, Any]) -> None:
        if not isinstance(sections, dict):
            raise ConfigError("platform configuration must be an object")
        unknown = set(sections) - PLATFORM_SECTION_NAMES
        if unknown:
            raise ConfigError(f"unsupported platform sections: {sorted(unknown)}")
        if not sections:
            raise ConfigError("at least one platform section is required")

    def _effective_config(self, file_config: dict[str, Any]) -> dict[str, Any]:
        effective = deepcopy(file_config)
        effective.update(deepcopy(self.managed_sections))
        return effective
