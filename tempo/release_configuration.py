"""Operator-reviewed configuration for one static artifact and named staging target."""

import os
import re
import uuid
from pathlib import Path

from django.db import transaction

from tempo.acceptance_contract import digest
from tempo.build_profiles import MINI_APP
from tempo.errors import CodexError, ConfigError
from tempo.preview_server import CSP
from tempo.release_files import ADAPTER
from tempo_web.artifact_views import verified_candidate_artifact
from tempo_web.models import BuildArtifact, DeploymentTarget, ReleaseConfiguration


def settings():
    root = os.getenv("TEMPO_RELEASE_ROOT", "")
    port = int(os.getenv("TEMPO_RELEASE_PORT", "8032"))
    if not root or not Path(root).is_absolute() or not 1024 <= port <= 65535:
        raise ValueError("Local staging is not configured on this installation")
    return Path(root), port


def specification(artifact, target):
    _root, port = settings()
    profile = artifact.manifest.get("build_profile")
    if profile != MINI_APP:
        raise ValueError("Local staging currently supports the React mini-app build profile")
    project_id = artifact.run.execution_plan.brief_revision.brief.project_id
    if target.project_id != project_id:
        raise ValueError("The deployment target belongs to a different project")
    return {
        "schema": 1,
        "adapter": ADAPTER,
        "environment": "local-staging",
        "target_id": target.pk,
        "target_key": target.key,
        "target_slot": target.slot.hex,
        "origin": f"http://{target.slot.hex}.localhost:{port}",
        "base_path": "/",
        "artifact_digest": artifact.digest,
        "manifest_digest": artifact.manifest_digest,
        "snapshot_digest": artifact.run.snapshot_digest,
        "build_profile_digest": digest(profile),
        "runtime": {"kind": "static", "variables": {}, "secret_references": [], "migrations": []},
        "browser_policy": CSP,
        "health_policy": "all-inventory-files-v1",
        "rollback_policy": "restore-previous-immutable-bundle-v1",
    }


@transaction.atomic
def approve(artifact_id, key, expected_configuration_id, user_id):
    if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", key):
        raise ValueError("Use a target name of 1–48 lowercase letters, digits, or hyphens")
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    verified_candidate_artifact(artifact)
    latest = artifact.release_configurations.order_by("-id").first()
    if (latest.pk if latest else 0) != expected_configuration_id:
        raise ValueError("Release configuration changed. Refresh before approving another version")
    target, _created = DeploymentTarget.objects.get_or_create(
        project_id=artifact.run.execution_plan.brief_revision.brief.project_id,
        key=key,
        defaults={"slot": uuid.uuid4(), "created_by_id": user_id},
    )
    saved = specification(artifact, target)
    if latest and latest.specification == saved and latest.digest == digest(saved):
        return latest
    return ReleaseConfiguration.objects.create(
        artifact=artifact,
        target=target,
        specification=saved,
        digest=digest(saved),
        approved_by_id=user_id,
    )


def summary(artifact):
    latest = artifact.release_configurations.order_by("-id").first()
    result = {
        "passed": False,
        "status": "Review a deployment target and its configuration",
        "configuration": latest,
    }
    if not latest:
        return result
    try:
        verified_candidate_artifact(artifact)
        if latest.specification != specification(
            artifact, latest.target
        ) or latest.digest != digest(latest.specification):
            raise ValueError("Saved release configuration changed")
    except (ValueError, KeyError, TypeError, OSError, CodexError, ConfigError):
        result["status"] = "Release configuration is unavailable or changed; review it again"
        return result
    result.update(passed=True, status="Configuration reviewed for local staging")
    return result
