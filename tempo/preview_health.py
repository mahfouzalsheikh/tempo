"""Durable preview probes and short-lived, deployment-bound health evidence."""

import os
import uuid
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from tempo.acceptance_contract import digest
from tempo.errors import CodexError, ConfigError
from tempo.preview_files import metadata
from tempo.preview_probe import TIMEOUT, VERSION, expected_report, probe
from tempo_web.artifact_views import verified_candidate_artifact
from tempo_web.models import BuildArtifact, PreviewDeployment, PreviewHealthAttempt
from tempo_web.preview_views import configuration

FRESHNESS = timedelta(minutes=5)
ERRORS = (ValueError, KeyError, TypeError, OSError, CodexError, ConfigError)


def generation(deployment):
    return digest(deployment.token.hex)


def endpoint():
    host = os.getenv("TEMPO_PREVIEW_HEALTH_HOST", "preview-server")
    port = int(os.getenv("TEMPO_PREVIEW_HEALTH_PORT", "8080"))
    if not host or any(c in host for c in "/\\@\r\n") or not 1 <= port <= 65535:
        raise ValueError("Preview health connection is not configured")
    return host, port


def identity(deployment):
    root, public_port = configuration()
    artifact = deployment.artifact
    if not deployment.active or deployment.expires_at <= timezone.now():
        raise ValueError("Start or renew the preview before checking its health")
    document = metadata(root, deployment.token.hex)
    expected = {
        "schema": 1,
        "sha256": artifact.digest,
        "expires_at": deployment.expires_at.timestamp(),
        "files": {item["path"]: item for item in artifact.manifest["files"]},
    }
    if document != expected:
        raise ValueError("Published preview metadata changed")
    # Invalidate receipts when the local published copy changes, without re-reading
    # large archives on every page render. HTTP verification still checks every byte.
    path = Path(root) / deployment.token.hex / "artifact.zip"
    stat = path.stat()
    return {
        "checker": VERSION,
        "generation": generation(deployment),
        "artifact_digest": artifact.digest,
        "manifest_digest": artifact.manifest_digest,
        "expires_at": deployment.expires_at.isoformat(),
        "public_port": public_port,
        "endpoint": list(endpoint()),
        "publication": {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "inode": stat.st_ino,
        },
    }


@transaction.atomic
def enqueue(artifact_id, expected_generation, request_key, user_id):
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    deployment = PreviewDeployment.objects.select_for_update().get(artifact=artifact)
    if generation(deployment) != expected_generation:
        raise ValueError("The preview changed. Refresh before checking its health")
    verified_candidate_artifact(artifact)
    saved = identity(deployment)
    existing = PreviewHealthAttempt.objects.filter(request_key=request_key).first()
    if existing:
        if (
            existing.deployment_id != deployment.pk
            or existing.requested_by_id != user_id
            or existing.identity != saved
        ):
            raise ValueError("This request key belongs to another preview check")
        return existing
    active = deployment.health_attempts.filter(status__in=["queued", "running"]).first()
    if active:
        if active.identity != saved:
            raise ValueError("An earlier preview check is still pending. Refresh after it finishes")
        return active
    return PreviewHealthAttempt.objects.create(
        deployment=deployment,
        request_key=request_key,
        requested_by_id=user_id,
        identity=saved,
    )


@transaction.atomic
def claim():
    attempt = (
        PreviewHealthAttempt.objects.select_for_update(skip_locked=True)
        .filter(status="queued")
        .order_by("id")
        .first()
    )
    if attempt:
        attempt.status, attempt.started_at = "running", timezone.now()
        attempt.deadline = attempt.started_at + timedelta(seconds=TIMEOUT + 15)
        attempt.lease_token = uuid.uuid4()
        attempt.save(update_fields=["status", "started_at", "deadline", "lease_token"])
    return attempt


@transaction.atomic
def finish(attempt, report=None, error=""):
    # Same lock order as preview lifecycle and enqueue; stop/renew cannot race a pass.
    artifact_id = attempt.deployment.artifact_id
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    deployment = PreviewDeployment.objects.select_for_update().get(pk=attempt.deployment_id)
    current = PreviewHealthAttempt.objects.select_for_update().get(pk=attempt.pk)
    if (
        current.status != "running"
        or current.lease_token != attempt.lease_token
        or current.deadline <= timezone.now()
    ):
        return False
    try:
        verified_candidate_artifact(artifact)
        if identity(deployment) != current.identity:
            raise ValueError("Preview changed during the check")
    except ERRORS:
        report, error = None, "The preview changed or became unavailable during the check."
        current.status = "interrupted"
    else:
        current.status = "passed" if report is not None else "failed"
    completed = timezone.now()
    if completed >= current.deadline:
        return False  # Artifact verification may itself outlast the remaining lease.
    if report is not None:
        if set(report) != {"identity", "checks", "worker_revision"} or (
            report["identity"] != current.identity
            or report["checks"] != expected_report(artifact.manifest["files"])
            or not isinstance(report["worker_revision"], str)
            or not report["worker_revision"]
        ):
            raise ValueError("Incomplete or mismatched preview health evidence")
        report = report | {"checked_at": completed.isoformat()}
        current.report, current.report_digest = report, digest(report)
    current.error, current.finished_at = error[:2000], completed
    current.save(update_fields=["status", "report", "report_digest", "error", "finished_at"])
    return True


def process(attempt):
    try:
        deployment = attempt.deployment
        verified_candidate_artifact(deployment.artifact)
        if identity(deployment) != attempt.identity:
            raise ValueError("Preview changed")
        host, port = endpoint()
        checks = probe(
            host,
            port,
            attempt.identity["public_port"],
            deployment.token.hex,
            deployment.artifact.manifest["files"],
        )
        finish(
            attempt,
            {
                "identity": attempt.identity,
                "checks": checks,
                "worker_revision": os.getenv("TEMPO_BUILD_REVISION", "development"),
            },
        )
    except Exception:
        # Never include bearer hostnames, response bodies, or network errors in UI/logs.
        finish(
            attempt,
            error="Preview health check failed. The service may be unavailable, "
            "a file or browser policy may differ, or the check timed out.",
        )


def recover():
    expired = list(
        PreviewHealthAttempt.objects.filter(
            status="running", deadline__lt=timezone.now()
        ).values_list("pk", flat=True)[:20]
    )
    if not expired:
        return  # Idle recovery must not acquire a SQLite write lock on every poll.
    PreviewHealthAttempt.objects.filter(
        pk__in=expired, status="running", deadline__lt=timezone.now()
    ).update(
        status="interrupted",
        finished_at=timezone.now(),
        error="Preview check worker stopped or its deadline expired. Start a fresh check.",
    )


def summary(artifact):
    deployment = PreviewDeployment.objects.filter(artifact=artifact).first()
    result = {"passed": False, "status": "Start a preview to check its health", "attempt": None}
    if not deployment:
        return result
    latest = deployment.health_attempts.order_by("-id").first()
    result.update(generation=generation(deployment), attempt=latest, status="Ready to check")
    try:
        saved = identity(deployment)
    except ERRORS:
        result["status"] = "Preview unavailable or changed; start or renew it before checking"
        return result
    result["can_check"] = True
    if not latest:
        return result
    result["status"] = latest.status
    if latest.identity != saved:
        result["status"] = "The preview changed; run a fresh health check"
    elif latest.status == "running" and latest.deadline <= timezone.now():
        result["status"] = "Preview check interrupted; refresh after worker recovery"
    elif latest.status == "passed":
        try:
            verified_candidate_artifact(artifact)
            report = latest.report
            if (
                digest(report) != latest.report_digest
                or not latest.finished_at
                or report["identity"] != saved
                or report["checks"] != expected_report(artifact.manifest["files"])
                or set(report) != {"identity", "checks", "worker_revision", "checked_at"}
                or report["checked_at"] != latest.finished_at.isoformat()
                or not isinstance(report["worker_revision"], str)
                or not report["worker_revision"]
            ):
                raise ValueError("Preview evidence changed")
            valid_until = min(deployment.expires_at, latest.finished_at + FRESHNESS)
            if latest.finished_at > timezone.now() or valid_until <= timezone.now():
                result["status"] = "Health evidence expired; run a fresh check"
            else:
                result.update(passed=True, valid_until=valid_until, report=report)
        except ERRORS:
            result["status"] = "Preview health evidence unavailable"
    return result
