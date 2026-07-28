from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from .config import ServiceConfig, build_config
from .domain import Issue, WorkflowDefinition
from .errors import WorkflowError

DEFAULT_PROMPT = "You are working on an issue from the configured tracker."


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


class WorkflowStore:
    """Keeps the last known-good dynamically reloadable workflow."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.definition: WorkflowDefinition | None = None
        self.config: ServiceConfig | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> tuple[WorkflowDefinition, ServiceConfig]:
        async with self._lock:
            definition = load_workflow(self.path)
            config = build_config(definition.config, definition.path)
            self.definition, self.config, self.last_error = definition, config, None
            return definition, config

    async def reload_if_changed(self) -> bool:
        async with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
                if self.definition and self.definition.mtime_ns == mtime:
                    return False
                definition = load_workflow(self.path)
                config = build_config(definition.config, definition.path)
            except Exception as exc:
                self.last_error = str(exc)
                return False
            self.definition, self.config, self.last_error = definition, config, None
            return True

    def current(self) -> tuple[WorkflowDefinition, ServiceConfig]:
        if not self.definition or not self.config:
            raise RuntimeError("workflow store is not initialized")
        return self.definition, self.config
