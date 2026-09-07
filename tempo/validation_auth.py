"""Host-to-runner authentication; never include this credential in job payloads."""

import os

from .credentials import process_environment, resolve_credential
from .errors import ConfigError

RUNNER_TOKEN_REFERENCE = "$TEMPO_VALIDATION_RUNNER_TOKEN"


def runner_token(reference: str = RUNNER_TOKEN_REFERENCE) -> str:
    value = resolve_credential(reference)
    if len(value) < 32 or not value.isascii() or any(character.isspace() for character in value):
        raise ConfigError(
            "validation runner credential must be at least 32 non-whitespace ASCII characters"
        )
    return value


def runner_environment() -> dict[str, str]:
    environment = process_environment()
    # Only the remote runner receives its operator-configured Docker execution endpoint.
    if os.getenv("DOCKER_HOST"):
        environment["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return environment
