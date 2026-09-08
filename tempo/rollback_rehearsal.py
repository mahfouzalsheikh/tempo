"""Durable, lease-fenced rollback rehearsals on private copies of staging targets."""

import os
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from tempo import release_configuration, release_files
from tempo.acceptance_contract import digest
from tempo.preview_health import ERRORS
from tempo.preview_probe import expected_report, probe
from tempo.release_probe import absent, absent_report
from tempo_web.artifact_views import verified_candidate_artifact
from tempo_web.models import (
    BuildArtifact,
    DeploymentTarget,
    ReleaseConfiguration,
    RollbackRehearsal,
)

VERSION = "rollback-rehearsal-v1"
DURATION = timedelta(seconds=180)
FRESHNESS = timedelta(hours=24)
ACTIVE = ["queued", "running", "cleanup_pending"]


class LeaseLost(ValueError):
    pass


def endpoint():
    host = os.getenv("TEMPO_RELEASE_HEALTH_HOST", "release-server")
    port = int(os.getenv("TEMPO_RELEASE_HEALTH_PORT", "8080"))
    if not host or any(c in host for c in "/\\@\r\n") or not 1 <= port <= 65535:
        raise ValueError("Staging health connection is not configured")
    return host, port


def checked_configuration(configuration):
    artifact = configuration.artifact
    verified_candidate_artifact(artifact)
    if configuration.specification != release_configuration.specification(
        artifact, configuration.target
    ) or configuration.digest != digest(configuration.specification):
        raise ValueError("Release configuration changed")
    return artifact


def baseline(configuration):
    root, _ = release_configuration.settings()
    current = release_files.pointer(root, configuration.target.slot.hex)
    if current is None:
        return None
    previous = (
        ReleaseConfiguration.objects.filter(
            target_id=configuration.target_id, digest=current["configuration_digest"]
        )
        .order_by("-id")
        .first()
    )
    if not previous:
        raise ValueError("The active target has no reviewed configuration")
    artifact = checked_configuration(previous)
    release_files.verify_bundle(
        root, current["release_id"], artifact.digest, previous.digest, artifact.manifest["files"]
    )
    return {
        "pointer": current,
        "configuration_id": previous.pk,
        "artifact_digest": artifact.digest,
        "manifest_digest": artifact.manifest_digest,
    }


def identity(configuration):
    artifact = checked_configuration(configuration)
    latest = artifact.release_configurations.order_by("-id").first()
    if latest.pk != configuration.pk:
        raise ValueError("A newer configuration requires another rehearsal")
    root, port = release_configuration.settings()
    return {
        "checker": VERSION,
        "configuration_id": configuration.pk,
        "configuration_digest": configuration.digest,
        "artifact_digest": artifact.digest,
        "manifest_digest": artifact.manifest_digest,
        "target_id": configuration.target_id,
        "target_slot": configuration.target.slot.hex,
        "public_port": port,
        "storage_root": str(root),
        "endpoint": list(endpoint()),
        "baseline": baseline(configuration),
    }


@transaction.atomic
def enqueue(artifact_id, configuration_id, expected_digest, request_key, user_id):
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    configuration = artifact.release_configurations.order_by("-id").first()
    if (
        not configuration
        or configuration.pk != configuration_id
        or configuration.digest != expected_digest
    ):
        raise ValueError("Staging configuration changed. Refresh before rehearsing rollback")
    DeploymentTarget.objects.select_for_update().get(pk=configuration.target_id)
    saved = identity(configuration)
    existing = RollbackRehearsal.objects.filter(request_key=request_key).first()
    if existing:
        if (
            existing.configuration_id != configuration.pk
            or existing.requested_by_id != user_id
            or existing.identity != saved
        ):
            raise ValueError("This request key belongs to another rehearsal")
        return existing
    active = configuration.target.rehearsals.filter(status__in=ACTIVE).first()
    if active:
        if active.identity != saved:
            raise ValueError("Another rehearsal for this target is still pending")
        return active
    return RollbackRehearsal.objects.create(
        configuration=configuration,
        target=configuration.target,
        request_key=request_key,
        requested_by_id=user_id,
        slot=uuid.uuid4(),
        bundle=uuid.uuid4(),
        identity=saved,
    )


@transaction.atomic
def claim():
    attempt = (
        RollbackRehearsal.objects.select_for_update(skip_locked=True)
        .filter(status="queued")
        .order_by("id")
        .first()
    )
    if attempt:
        attempt.status, attempt.started_at = "running", timezone.now()
        attempt.deadline, attempt.lease_token = attempt.started_at + DURATION, uuid.uuid4()
        attempt.save(update_fields=["status", "started_at", "deadline", "lease_token"])
    return attempt


def owned(attempt, current):
    if (
        current.status != "running"
        or current.lease_token != attempt.lease_token
        or current.deadline <= timezone.now()
    ):
        raise LeaseLost("Rehearsal worker lost its lease")


def operation(attempt, stage):
    return uuid.uuid5(attempt.request_key, stage).hex


def private_slot(attempt):
    if DeploymentTarget.objects.filter(slot=attempt.slot).exists():
        raise ValueError("Rehearsal cannot use a named staging target")
    if (
        attempt.identity["baseline"]
        and attempt.identity["baseline"]["pointer"]["release_id"] == attempt.bundle.hex
    ):
        raise ValueError("Rehearsal cannot overwrite a retained release")


def cleanup(attempt):
    # Call only while holding this attempt's row lock. Never unlink lock files: another
    # process may still hold that inode. Cleanup cannot race a fenced activation.
    private_slot(attempt)
    root = Path(attempt.identity["storage_root"])
    current = release_files.pointer(root, attempt.slot.hex)
    if current:
        if current["operation_id"] not in {
            operation(attempt, stage) for stage in ("baseline", "candidate", "restore")
        }:
            raise ValueError("Unexpected rehearsal owner; cleanup needs intervention")
        release_files.remove_target(root, attempt.slot.hex, current["operation_id"])
    # This UUID is created only for this attempt; previous target bundles are retained.
    bundle = root / "bundles" / attempt.bundle.hex
    if bundle.exists():
        shutil.rmtree(bundle)
        release_files.sync_directory(bundle.parent)


@transaction.atomic
def stage(attempt, name):
    artifact = BuildArtifact.objects.select_for_update().get(pk=attempt.configuration.artifact_id)
    DeploymentTarget.objects.select_for_update().get(pk=attempt.target_id)
    current = RollbackRehearsal.objects.select_for_update().get(pk=attempt.pk)
    owned(attempt, current)
    if identity(current.configuration) != current.identity:
        raise ValueError("Target or release configuration changed during rehearsal")
    private_slot(current)
    root = Path(current.identity["storage_root"])
    prior = current.identity["baseline"]
    # Recheck the deadline after expensive verification, immediately before effects.
    owned(attempt, current)
    if name == "prepare":
        bundle = root / "bundles" / current.bundle.hex
        if bundle.exists():
            release_files.verify_bundle(
                root,
                current.bundle.hex,
                artifact.digest,
                current.configuration.digest,
                artifact.manifest["files"],
            )
        else:
            release_files.prepare(
                root,
                current.bundle.hex,
                bytes(artifact.data),
                artifact.manifest["files"],
                artifact.digest,
                current.configuration.digest,
            )
    elif name == "baseline" and prior:
        release_files.activate(
            root,
            current.slot.hex,
            prior["pointer"]["release_id"],
            prior["pointer"]["configuration_digest"],
            None,
            operation(current, "baseline"),
        )
    elif name == "candidate":
        release_files.activate(
            root,
            current.slot.hex,
            current.bundle.hex,
            current.configuration.digest,
            operation(current, "baseline") if prior else None,
            operation(current, "candidate"),
        )
    elif name == "restore":
        if prior:
            release_files.activate(
                root,
                current.slot.hex,
                prior["pointer"]["release_id"],
                prior["pointer"]["configuration_digest"],
                operation(current, "candidate"),
                operation(current, "restore"),
            )
        else:
            release_files.remove_target(root, current.slot.hex, operation(current, "candidate"))
    elif name not in {"baseline"}:
        raise ValueError("Unknown rehearsal stage")


def reports(attempt):
    candidate = expected_report(attempt.configuration.artifact.manifest["files"])
    prior = attempt.identity["baseline"]
    if prior:
        artifact = ReleaseConfiguration.objects.get(pk=prior["configuration_id"]).artifact
        restoration = expected_report(artifact.manifest["files"])
    else:
        restoration = absent_report()
    return {"candidate": candidate, "restoration": restoration, "cleanup": absent_report()}


@transaction.atomic
def complete(attempt, checks=None, error=""):
    BuildArtifact.objects.select_for_update().get(pk=attempt.configuration.artifact_id)
    DeploymentTarget.objects.select_for_update().get(pk=attempt.target_id)
    current = RollbackRehearsal.objects.select_for_update().get(pk=attempt.pk)
    owned(attempt, current)
    if checks is not None:
        if identity(current.configuration) != current.identity or checks != reports(current):
            raise ValueError("Rehearsal evidence or target changed")
    cleanup(current)
    owned(attempt, current)
    current.finished_at = timezone.now()
    current.status = "passed" if checks is not None else "failed"
    if checks is not None:
        current.report = {
            "identity": current.identity,
            "checks": checks,
            "worker_revision": os.getenv("TEMPO_BUILD_REVISION", "development"),
            "checked_at": current.finished_at.isoformat(),
        }
        current.report_digest = digest(current.report)
    current.error = error
    current.save(update_fields=["status", "finished_at", "report", "report_digest", "error"])


@transaction.atomic
def revoke(attempt):
    current = RollbackRehearsal.objects.select_for_update().get(pk=attempt.pk)
    owned(attempt, current)
    cleanup(current)


def process(attempt):
    try:
        stage(attempt, "prepare")
        stage(attempt, "baseline")
        stage(attempt, "candidate")
        host, port = attempt.identity["endpoint"]
        public_port, slot = attempt.identity["public_port"], attempt.slot.hex
        candidate = probe(
            host, port, public_port, slot, attempt.configuration.artifact.manifest["files"]
        )
        stage(attempt, "restore")
        prior = attempt.identity["baseline"]
        if prior:
            artifact = ReleaseConfiguration.objects.get(pk=prior["configuration_id"]).artifact
            restoration = probe(host, port, public_port, slot, artifact.manifest["files"])
        else:
            restoration = absent(host, port, public_port, slot)
        revoke(attempt)
        removed = absent(host, port, public_port, slot)
        complete(attempt, {"candidate": candidate, "restoration": restoration, "cleanup": removed})
    except LeaseLost:
        return  # Recovery owns cleanup; stale workers cannot create further side effects.
    except Exception:
        try:
            complete(
                attempt,
                error="Rollback rehearsal failed or the target changed. "
                "Refresh and retry after checking staging health.",
            )
        except LeaseLost:
            pass
        except Exception:
            # Leave durable running intent for recovery; never report success without cleanup.
            pass


def recover():
    ids = list(
        RollbackRehearsal.objects.filter(
            status__in=["running", "cleanup_pending"], deadline__lt=timezone.now()
        ).values_list("pk", flat=True)[:20]
    )
    for pk in ids:
        with transaction.atomic():
            current = RollbackRehearsal.objects.select_for_update().get(pk=pk)
            if (
                current.status not in {"running", "cleanup_pending"}
                or current.deadline >= timezone.now()
            ):
                continue
            try:
                cleanup(current)
            except Exception:
                current.status = "cleanup_pending"
                current.deadline = timezone.now() + timedelta(seconds=30)
                current.error = (
                    "Rehearsal cleanup is pending. This target cannot start another rehearsal yet."
                )
            else:
                current.status, current.finished_at = "interrupted", timezone.now()
                current.error = (
                    "Rehearsal worker stopped or its deadline expired. Start a fresh rehearsal."
                )
            current.save(update_fields=["status", "deadline", "finished_at", "error"])


def summary(artifact):
    result = {
        "passed": False,
        "status": "Approve staging configuration before rehearsing rollback",
        "attempt": None,
    }
    configuration = artifact.release_configurations.order_by("-id").first()
    if not configuration:
        return result
    try:
        saved = identity(configuration)
        result.update(
            can_rehearse=True, configuration=configuration, status="Ready to rehearse rollback"
        )
        latest = configuration.target.rehearsals.order_by("-id").first()
        result["attempt"] = latest
        if not latest:
            return result
        result["status"] = latest.status
        if latest.status in ACTIVE:
            result["can_rehearse"] = False
        if latest.identity != saved:
            result["status"] = "The target or configuration changed; run a fresh rehearsal"
        elif latest.status == "passed":
            report = latest.report
            if (
                set(report) != {"identity", "checks", "worker_revision", "checked_at"}
                or report["identity"] != saved
                or report["checks"] != reports(latest)
                or digest(report) != latest.report_digest
                or not latest.finished_at
                or report["checked_at"] != latest.finished_at.isoformat()
                or not isinstance(report["worker_revision"], str)
                or not report["worker_revision"]
            ):
                raise ValueError("Rehearsal receipt changed")
            valid_until = latest.finished_at + FRESHNESS
            if latest.finished_at > timezone.now() or valid_until <= timezone.now():
                result["status"] = "Rehearsal evidence expired; run a fresh rehearsal"
            else:
                result.update(
                    passed=True,
                    valid_until=valid_until,
                    report=report,
                    status="Rollback rehearsed for this build and target",
                )
        elif latest.status in ACTIVE:
            result["can_rehearse"] = False
    except ERRORS:
        result.update(
            passed=False,
            can_rehearse=False,
            status="Rehearsal evidence or staging target is unavailable or changed",
        )
    return result
