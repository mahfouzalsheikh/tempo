from __future__ import annotations

from typing import Any

_orchestrator: Any | None = None


def set_orchestrator(orchestrator: Any | None) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> Any | None:
    return _orchestrator
