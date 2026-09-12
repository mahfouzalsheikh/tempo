"""Nonsecret, versioned account references embedded in execution snapshots."""

import re
from contextvars import ContextVar
from uuid import UUID

SELECTED_ACCOUNT = ContextVar("selected_agent_account", default=None)


def validate_binding(value):
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "id",
        "project_id",
        "identity",
        "generation",
    }:
        raise ValueError("Invalid account assignment.")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("Unsupported account assignment.")
    if str(UUID(value["id"])) != value["id"]:
        raise ValueError("Invalid account ID.")
    if any(type(value[k]) is not int or value[k] < 1 for k in ("project_id", "generation")):
        raise ValueError("Invalid account assignment version.")
    if not isinstance(value["identity"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["identity"]
    ):
        raise ValueError("Invalid account identity.")
    return value


def has_bindings(config):
    return any("account" in p.settings for p in getattr(config, "agents", {}).values())
