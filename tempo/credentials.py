"""Environment references stay declarative until a credential consumer needs them."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from .errors import ConfigError

ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REFERENCE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")
TRACKER_CREDENTIAL_KEYS = ("token", "api_key", "review_token")
BASE_ENVIRONMENT = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"})
CONTROL_PLANE_SECRETS = frozenset({
    "GITHUB_TOKEN", "GITHUB_REVIEW_TOKEN", "DJANGO_SECRET_KEY", "TEMPO_ADMIN_PASSWORD",
    "DATABASE_URL", "POSTGRES_PASSWORD", "TEMPO_POSTGRES_PASSWORD", "TEMPO_VALIDATION_RUNNER_TOKEN",
})
# Even explicit grants must not source shell startup scripts or inject a loader.
UNSAFE_ENVIRONMENT = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH"})


def reference_name(value: str) -> str:
    if not isinstance(value, str) or not REFERENCE.fullmatch(value):
        raise ConfigError("credentials must use a $ENVIRONMENT_VARIABLE reference")
    return value[1:]


def resolve_credential(reference: str, *, required: bool = True) -> str | None:
    name = reference_name(reference)
    value = os.environ.get(name)
    if not value and required:
        raise ConfigError(f"credential environment variable {name} is not set")
    return value or None


def process_environment(
    references: Mapping[str, str] | None = None, *,
    forbidden: set[str] | frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Build a scoped environment without inheriting unknown server variables.

    HOME and PATH remain operator-controlled compatibility inputs, not isolation boundaries.
    Callers running repository code must forbid control-plane and tracker credentials.
    """
    environment = {
        name: os.environ[name] for name in BASE_ENVIRONMENT - forbidden if name in os.environ
    }
    environment.setdefault("PATH", os.defpath)
    for target, reference in (references or {}).items():
        source = reference_name(reference)
        if not ENVIRONMENT_NAME.fullmatch(target):
            raise ConfigError("subprocess environment keys must be valid variable names")
        if target in forbidden or source in forbidden:
            raise ConfigError("control-plane credentials cannot be granted to this subprocess")
        if target in UNSAFE_ENVIRONMENT or target.startswith(("LD_", "DYLD_", "BASH_FUNC_")):
            raise ConfigError("shell startup and loader variables cannot be granted")
        environment[target] = resolve_credential(reference)
    return environment


def redact_credentials(text: str, values: list[str]) -> str:
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return text
