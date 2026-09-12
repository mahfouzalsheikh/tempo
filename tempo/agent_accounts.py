"""Staff-managed account registry. This slice does not change runtime routing."""

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from tempo_web.models import AgentAccount, AgentAccountEvent, AgentAccountGrant, Project

LIMIT = 1024 * 1024


def administrator(user):
    if not user.is_active or not user.is_staff:
        raise PermissionDenied("Account administration requires a staff operator.")


def event(account, user, action, **detail):
    AgentAccountEvent.objects.create(
        account=account,
        actor=user,
        action=action,
        revision=account.revision,
        detail=detail,
    )


@transaction.atomic
def create(user, organization, label, auth_mode):
    administrator(user)
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
        raise ValueError("Name the connection using 1–100 characters.")
    if auth_mode not in {"codex-file", "installation"}:
        raise ValueError("Select a supported Codex connection method.")
    account = AgentAccount.objects.create(
        organization=organization,
        label=label.strip(),
        auth_mode=auth_mode,
        created_by=user,
    )
    event(account, user, "created")
    return account


def locked(account_id, revision):
    account = AgentAccount.objects.select_for_update().get(pk=account_id)
    if account.revision != revision:
        raise ValueError("This connection changed. Reload before trying again.")
    return account


@transaction.atomic
def update(user, account_id, revision, *, disabled, project_ids):
    administrator(user)
    account = locked(account_id, revision)
    if type(disabled) is not bool or not isinstance(project_ids, list):
        raise ValueError("Invalid connection settings.")
    if any(type(pk) is not int for pk in project_ids):
        raise ValueError("Select valid projects.")
    projects = list(
        Project.objects.filter(
            pk__in=project_ids,
            organization=account.organization,
            active=True,
        )
    )
    if len(projects) != len(set(project_ids)):
        raise ValueError("Projects must be active and belong to this organization.")
    account.disabled = disabled
    account.revision += 1
    account.save(update_fields=["disabled", "revision"])
    account.grants.exclude(project_id__in=project_ids).update(active=False)
    for project in projects:
        AgentAccountGrant.objects.update_or_create(
            account=account,
            project=project,
            defaults={"active": True},
        )
    event(
        account, user, "settings_updated", disabled=disabled, project_ids=sorted(set(project_ids))
    )
    return account


def read_cache(path):
    """Refuse links, devices, oversized and group/world-accessible credential files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > LIMIT:
            raise ValueError("Use a private regular Codex login file, at most 1 MiB.")
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Codex login file is too large.")
    return data


def cache_identity(data):
    """Local identity claims only; this deliberately makes no server-authentication claim."""
    try:
        value = json.loads(data)
        tokens = value["tokens"]
        if value.get("OPENAI_API_KEY") or value.get("auth_mode") not in {None, "chatgpt"}:
            raise ValueError
        for key in ["account_id", "id_token", "access_token", "refresh_token"]:
            if not isinstance(tokens[key], str) or not tokens[key]:
                raise ValueError
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject:
            raise ValueError
        return hashlib.sha256(json.dumps([tokens["account_id"], subject]).encode()).hexdigest()
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise ValueError("A supported Codex ChatGPT login cache is required.") from exc


def credential_root():
    value = os.environ.get("TEMPO_ACCOUNT_CREDENTIAL_ROOT", "")
    if not value or not Path(value).is_absolute():
        raise ValueError("The account credential store is not configured.")
    root = Path(value)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError("The account credential store must be a private directory.")
    return root


def stored_cache(account):
    if account.auth_mode == "installation":
        return read_cache(Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json")
    root = credential_root()
    # Account IDs are UUIDs, never user-supplied filesystem paths.
    return read_cache(root / f"{account.pk.hex}.json")


@transaction.atomic
def provision(user, account_id, revision, source):
    administrator(user)
    account = locked(account_id, revision)
    if account.auth_mode != "codex-file" or account.credential_generation:
        raise ValueError(
            "Provision a new file connection. Credential replacement is not supported."
        )
    data = read_cache(source)
    identity = cache_identity(data)
    root = credential_root()
    path = root / f"{account.pk.hex}.json"
    # No overwrite: a failed/ambiguous provision requires administrator reconciliation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    account.identity_fingerprint = identity
    account.credential_generation = 1
    account.check_status = "stored"
    account.checked_at = timezone.now()
    account.revision += 1
    account.save()
    event(account, user, "provisioned", generation=1)
    return account


@transaction.atomic
def check(user, account_id, revision):
    administrator(user)
    account = locked(account_id, revision)
    try:
        identity = cache_identity(stored_cache(account))
        if account.identity_fingerprint and account.identity_fingerprint != identity:
            raise ValueError("Identity changed")
        account.identity_fingerprint = identity
        account.check_status = "stored"
        account.credential_generation = max(1, account.credential_generation)
    except (ValueError, OSError):
        account.check_status = "unavailable"
    account.checked_at = timezone.now()
    account.revision += 1
    account.save()
    event(account, user, "cache_checked", status=account.check_status)
    return account


def public_record(account):
    return {
        "id": str(account.pk),
        "organization_id": account.organization_id,
        "label": account.label,
        "provider": account.provider,
        "auth_mode": account.auth_mode,
        "disabled": account.disabled,
        "revision": account.revision,
        "credential_generation": account.credential_generation,
        "identity_hint": account.identity_fingerprint[:12],
        "check_status": account.check_status,
        "checked_at": account.checked_at,
        "project_ids": list(
            account.grants.filter(active=True).values_list(
                "project_id",
                flat=True,
            )
        ),
        "runtime_routing": "not_available",
        "authentication_verified": False,
    }
