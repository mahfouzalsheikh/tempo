from __future__ import annotations

from .orchestrator import Orchestrator

_orchestrator: Orchestrator | None = None


def set_orchestrator(orchestrator: Orchestrator | None) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> Orchestrator | None:
    return _orchestrator
