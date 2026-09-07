class TempoError(Exception):
    """Base error carrying a stable, operator-visible category."""

    category = "tempo_error"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category:
            self.category = category


class WorkflowError(TempoError):
    category = "workflow_parse_error"


class ConfigError(TempoError):
    category = "config_validation_error"


class TrackerError(TempoError):
    category = "tracker_error"


class WorkspaceError(TempoError):
    category = "workspace_error"


class CodexError(TempoError):
    category = "codex_error"


class LeaseLostError(TempoError):
    category = "lease_lost"
