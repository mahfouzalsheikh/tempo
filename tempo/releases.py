"""Durable publication of verified static bundles, with fenced compensating recovery."""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from tempo import release_configuration, release_files, rollback_rehearsal
from tempo.acceptance_contract import digest
from tempo.preview_probe import expected_report, probe
from tempo.release_probe import absent, absent_report
from tempo_web.artifact_views import verified_candidate_artifact
from tempo_web.models import (
    BuildArtifact,
    DeploymentTarget,
    ProductBrief,
    ReleaseDeployment,
)

ACTIVE = [
    "queued",
    "running",
    "rollback_queued",
    "recovering",
    "recovery_pending",
    "needs_attention",
]
DURATION = timedelta(seconds=180)
ERRORS = rollback_rehearsal.ERRORS
LeaseLost = rollback_rehearsal.LeaseLost


def operation(release, name):
    return uuid.uuid5(release.request_key, name).hex


def candidate_pointer(release):
    return {
        "schema": 1,
        "slot": release.identity["destination"]["target_slot"],
        "release_id": release.bundle.hex,
        "configuration_digest": release.identity["configuration_digest"],
        "operation_id": operation(release, "publish"),
    }


def baseline_pointer(release):
    saved = release.identity["destination"]["baseline"]
    return saved["pointer"] if saved else None


def restored_pointer(release):
    prior = baseline_pointer(release)
    return prior | {"operation_id": operation(release, "restore")} if prior else None


def checked_intent(release):
    saved = release.identity
    if (
        digest(saved) != release.identity_digest
        or saved["version"] != "static-publication-v1"
        or saved["configuration_id"] != release.configuration_id
        or saved["target_id"] != release.target_id
        or saved["resources"]
        != digest({"bundle": release.bundle.hex, "probe_slot": release.probe_slot.hex})
        or saved["request_key"] != release.request_key.hex
    ):
        raise ValueError("Release intent changed")
    root, port = release_configuration.settings()
    destination = saved["destination"]
    if (
        str(root) != destination["storage_root"]
        or port != destination["public_port"]
        or list(rollback_rehearsal.endpoint()) != destination["endpoint"]
        or release.target.slot.hex != destination["target_slot"]
        or DeploymentTarget.objects.filter(slot=release.probe_slot).exists()
    ):
        raise ValueError("Release destination changed")
    return root


def evidence(artifact, *, activated=False):
    # Import lazily: the web assessment uses this module only for controls/history.
    from tempo_web.release_views import evaluate

    if not artifact.run.execution_plan.brief_revision.brief.project.active:
        raise ValueError("Project is inactive")
    report = evaluate(artifact)
    required = [g for g in report["gates"] if not activated or g["id"] != "rollback"]
    if not all(g["status"] == "passed" for g in required):
        raise ValueError("Release gates need fresh passing evidence")
    return report


def fresh_until(report, *, activated=False):
    keys = ["preview_health"] if activated else ["preview_health", "rollback_rehearsal"]
    if any(datetime.fromisoformat(report[key]["valid_until"]) <= timezone.now() for key in keys):
        raise ValueError("Release evidence expired before the final write")


def evidence_ids(report):
    return {
        "artifact_digest": report["artifact_digest"],
        "manifest_digest": report["manifest_digest"],
        "snapshot_digest": report["snapshot_digest"],
        "plan_id": report["plan_id"],
        "configuration_id": report["release_configuration"]["id"],
        "configuration_digest": report["release_configuration"]["digest"],
        "acceptance_id": report["acceptance"]["attempt_id"],
        "acceptance_digest": report["acceptance"]["report_digest"],
        "health_id": report["preview_health"]["attempt_id"],
        "health_digest": report["preview_health"]["report_digest"],
    }


def checked_evidence(release, activated=False):
    report = evidence(release.configuration.artifact, activated=activated)
    if evidence_ids(report) != release.identity["evidence"]:
        raise ValueError("Release evidence changed after publication was requested")
    if not activated:
        rehearsal = report["rollback_rehearsal"]
        if (
            rehearsal["attempt_id"] != release.identity["rehearsal_id"]
            or rehearsal["report_digest"] != release.identity["rehearsal_digest"]
            or rollback_rehearsal.identity(release.configuration) != release.identity["destination"]
        ):
            raise ValueError("Rollback rehearsal or target changed")
    else:
        # The target changed by this publication, so the pre-activation rehearsal's
        # baseline no longer describes current readiness. Verify the frozen receipt.
        latest = release.target.rehearsals.order_by("-id").first()
        saved = release.authorization["assessment"]["rollback_rehearsal"]["report"]
        if (
            not latest
            or latest.pk != release.identity["rehearsal_id"]
            or latest.status != "passed"
            or latest.report != saved
            or latest.report_digest != release.identity["rehearsal_digest"]
            or digest(latest.report) != latest.report_digest
            or latest.finished_at + rollback_rehearsal.FRESHNESS <= timezone.now()
        ):
            raise ValueError("Authorized rollback receipt changed")
    return report


@contextmanager
def locked(release):
    brief_id = release.configuration.artifact.run.execution_plan.brief_revision.brief_id
    with transaction.atomic():
        ProductBrief.objects.select_for_update().get(pk=brief_id)
        BuildArtifact.objects.select_for_update().get(pk=release.configuration.artifact_id)
        DeploymentTarget.objects.select_for_update().get(pk=release.target_id)
        yield ReleaseDeployment.objects.select_for_update().get(pk=release.pk)


def owned(worker, current, status="running"):
    if (
        current.status != status
        or current.lease_token != worker.lease_token
        or current.deadline <= timezone.now()
    ):
        raise LeaseLost("Release worker lost its lease")


@transaction.atomic
def enqueue(
    artifact_id, configuration_id, configuration_digest, expected_target, request_key, user_id
):
    artifact = BuildArtifact.objects.get(pk=artifact_id)
    ProductBrief.objects.select_for_update().get(
        pk=artifact.run.execution_plan.brief_revision.brief_id
    )
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    configuration = artifact.release_configurations.order_by("-id").first()
    if (
        not configuration
        or configuration.pk != configuration_id
        or configuration.digest != configuration_digest
    ):
        raise ValueError("Staging configuration changed")
    target = DeploymentTarget.objects.select_for_update().get(pk=configuration.target_id)
    existing = ReleaseDeployment.objects.filter(request_key=request_key).first()
    if existing:
        if existing.configuration_id != configuration.pk or existing.requested_by_id != user_id:
            raise ValueError("This request belongs to another publication")
        checked_intent(existing)
        return existing  # Lost response after publication must never queue another effect.
    if (
        target.releases.filter(status__in=ACTIVE).exists()
        or target.rehearsals.filter(status__in=rollback_rehearsal.ACTIVE).exists()
    ):
        raise ValueError("This staging target has an operation in progress")
    report = evidence(artifact)
    destination = rollback_rehearsal.identity(configuration)
    if digest(destination["baseline"]) != expected_target:
        raise ValueError("The staging target changed; review it before publishing")
    bundle, probe_slot = uuid.uuid4(), uuid.uuid4()
    saved = {
        "version": "static-publication-v1",
        "configuration_id": configuration.pk,
        "configuration_digest": configuration.digest,
        "target_id": target.pk,
        "resources": digest({"bundle": bundle.hex, "probe_slot": probe_slot.hex}),
        "request_key": request_key.hex,
        "destination": destination,
        "evidence": evidence_ids(report),
        "rehearsal_id": report["rollback_rehearsal"]["attempt_id"],
        "rehearsal_digest": report["rollback_rehearsal"]["report_digest"],
    }
    return ReleaseDeployment.objects.create(
        configuration=configuration,
        target=target,
        request_key=request_key,
        requested_by_id=user_id,
        bundle=bundle,
        probe_slot=probe_slot,
        identity=saved,
        identity_digest=digest(saved),
    )


@transaction.atomic
def claim():
    job = (
        ReleaseDeployment.objects.select_for_update(skip_locked=True)
        .filter(status__in=["queued", "rollback_queued", "recovery_pending"])
        .filter(models_ready())
        .order_by("id")
        .first()
    )
    if job:
        job.status = "running" if job.status == "queued" else "recovering"
        job.started_at = job.started_at or timezone.now()
        job.deadline, job.lease_token = timezone.now() + DURATION, uuid.uuid4()
        if job.status == "recovering":
            job.recovery_attempts += 1
        job.save(
            update_fields=["status", "started_at", "deadline", "lease_token", "recovery_attempts"]
        )
    return job


def models_ready():
    from django.db.models import Q

    return ~Q(status="recovery_pending") | Q(deadline__lte=timezone.now())


def verify_candidate(release, root):
    artifact = release.configuration.artifact
    verified_candidate_artifact(artifact)
    if (
        artifact.digest != release.identity["evidence"]["artifact_digest"]
        or artifact.manifest_digest != release.identity["evidence"]["manifest_digest"]
    ):
        raise ValueError("Release artifact changed")
    release_files.verify_bundle(
        root,
        release.bundle.hex,
        artifact.digest,
        release.identity["configuration_digest"],
        artifact.manifest["files"],
    )
    return artifact


def prepare(worker):
    with locked(worker) as current:
        owned(worker, current)
        root = checked_intent(current)
        checked_evidence(current)
        artifact = current.configuration.artifact
        owned(worker, current)
        if (root / "bundles" / current.bundle.hex).exists():
            verify_candidate(current, root)
        else:
            release_files.prepare(
                root,
                current.bundle.hex,
                bytes(artifact.data),
                artifact.manifest["files"],
                artifact.digest,
                current.identity["configuration_digest"],
                before_commit=lambda: owned(worker, current),
            )
        owned(worker, current)
        release_files.activate(
            root,
            current.probe_slot.hex,
            current.bundle.hex,
            current.identity["configuration_digest"],
            None,
            operation(current, "preflight"),
            before_commit=lambda: owned(worker, current),
        )


def activate(worker, preflight):
    with locked(worker) as current:
        owned(worker, current)
        root = checked_intent(current)
        report = checked_evidence(current)
        artifact = verify_candidate(current, root)
        if preflight != expected_report(artifact.manifest["files"]):
            raise ValueError("Incomplete preflight health receipt")
        if release_files.pointer(root, current.probe_slot.hex) != candidate_pointer(current) | {
            "slot": current.probe_slot.hex,
            "operation_id": operation(current, "preflight"),
        }:
            raise ValueError("Preflight publication changed")
        if release_files.pointer(root, current.target.slot.hex) != baseline_pointer(current):
            raise ValueError("Staging target changed")
        owned(worker, current)
        current.authorization = {
            "assessment": report,
            "preflight": preflight,
            "authorized_at": timezone.now().isoformat(),
        }
        current.authorization_digest = digest(current.authorization)
        current.save(update_fields=["authorization", "authorization_digest"])
        prior = baseline_pointer(current)

        def authorize():
            owned(worker, current)
            fresh_until(report)

        release_files.activate(
            root,
            current.target.slot.hex,
            current.bundle.hex,
            current.identity["configuration_digest"],
            prior["operation_id"] if prior else None,
            operation(current, "publish"),
            before_commit=authorize,
        )
        # Queue intent was committed before this effect. A crash before this transaction
        # commits leaves an unconfirmed publication which recovery will compensate.


def revoke_preflight(current, root, before_commit=None):
    release_files.remove_target(
        root, current.probe_slot.hex, operation(current, "preflight"), before_commit=before_commit
    )


def cleanup_preflight(worker):
    with locked(worker) as current:
        owned(worker, current)
        root = checked_intent(current)
        revoke_preflight(current, root, lambda: owned(worker, current))


def finish(worker, checks, cleanup_check):
    with locked(worker) as current:
        owned(worker, current)
        root = checked_intent(current)
        if digest(current.authorization) != current.authorization_digest:
            raise ValueError("Release authorization changed")
        assessment = checked_evidence(current, activated=True)
        artifact = verify_candidate(current, root)
        if (
            checks != expected_report(artifact.manifest["files"])
            or cleanup_check != absent_report()
            or release_files.pointer(root, current.target.slot.hex) != candidate_pointer(current)
            or release_files.pointer(root, current.probe_slot.hex) is not None
        ):
            raise ValueError("Published release health or target changed")
        owned(worker, current)
        fresh_until(assessment, activated=True)
        current.finished_at = timezone.now()
        current.report = {
            "identity_digest": current.identity_digest,
            "authorization_digest": current.authorization_digest,
            "pointer": candidate_pointer(current),
            "checks": checks,
            "cleanup": cleanup_check,
            "checked_at": current.finished_at.isoformat(),
            "worker_revision": os.getenv("TEMPO_BUILD_REVISION", "development"),
        }
        current.report_digest = digest(current.report)
        current.status, current.error = "published", ""
        current.save(update_fields=["status", "finished_at", "report", "report_digest", "error"])


def schedule_recovery(worker):
    with locked(worker) as current:
        owned(worker, current)
        current.status = "rollback_queued"
        current.error = (
            "Publication was interrupted or a check failed. "
            "Recovery will verify the previous target state."
        )
        current.save(update_fields=["status", "error"])


def restored_artifact(current):
    prior = current.identity["destination"]["baseline"]
    if not prior:
        return None
    from tempo_web.models import ReleaseConfiguration

    configuration = ReleaseConfiguration.objects.get(pk=prior["configuration_id"])
    artifact = rollback_rehearsal.checked_configuration(configuration)
    if (
        configuration.target_id != current.target_id
        or configuration.digest != prior["pointer"]["configuration_digest"]
        or artifact.digest != prior["artifact_digest"]
        or artifact.manifest_digest != prior["manifest_digest"]
    ):
        raise ValueError("Previous release identity changed")
    release_files.verify_bundle(
        Path(current.identity["destination"]["storage_root"]),
        prior["pointer"]["release_id"],
        artifact.digest,
        configuration.digest,
        artifact.manifest["files"],
    )
    return artifact


def restore(worker):
    with locked(worker) as current:
        owned(worker, current, "recovering")
        root = checked_intent(current)
        previous = restored_artifact(current)
        actual = release_files.pointer(root, current.target.slot.hex)
        prior, restored = baseline_pointer(current), restored_pointer(current)
        if actual not in (prior, restored, candidate_pointer(current)):
            raise ValueError(
                "Staging pointer has an unrecognized owner; operator recovery required"
            )
        owned(worker, current, "recovering")
        if actual == candidate_pointer(current):
            if previous:
                release_files.activate(
                    root,
                    current.target.slot.hex,
                    prior["release_id"],
                    prior["configuration_digest"],
                    operation(current, "publish"),
                    operation(current, "restore"),
                    before_commit=lambda: owned(worker, current, "recovering"),
                )
            else:
                release_files.remove_target(
                    root,
                    current.target.slot.hex,
                    operation(current, "publish"),
                    before_commit=lambda: owned(worker, current, "recovering"),
                )
        revoke_preflight(current, root, lambda: owned(worker, current, "recovering"))
        return previous


def finish_recovery(worker, checks, cleanup_check):
    with locked(worker) as current:
        owned(worker, current, "recovering")
        root = checked_intent(current)
        artifact = restored_artifact(current)
        expected = expected_report(artifact.manifest["files"]) if artifact else absent_report()
        actual = release_files.pointer(root, current.target.slot.hex)
        if (
            checks != expected
            or cleanup_check != absent_report()
            or actual not in (baseline_pointer(current), restored_pointer(current))
            or release_files.pointer(root, current.probe_slot.hex) is not None
        ):
            raise ValueError("Previous target state could not be verified")
        owned(worker, current, "recovering")
        current.recovery_report = {
            "identity_digest": current.identity_digest,
            "pointer": actual,
            "checks": checks,
            "cleanup": cleanup_check,
            "checked_at": timezone.now().isoformat(),
            "worker_revision": os.getenv("TEMPO_BUILD_REVISION", "development"),
        }
        current.recovery_digest = digest(current.recovery_report)
        current.status = "rolled_back"
        current.save(update_fields=["status", "recovery_report", "recovery_digest"])


def process(worker):
    try:
        destination = worker.identity["destination"]
        host, port = destination["endpoint"]
        public_port = destination["public_port"]
        if worker.status == "recovering":
            artifact = restore(worker)
            checks = (
                probe(
                    host, port, public_port, destination["target_slot"], artifact.manifest["files"]
                )
                if artifact
                else absent(host, port, public_port, destination["target_slot"])
            )
            cleanup_check = absent(host, port, public_port, worker.probe_slot.hex)
            finish_recovery(worker, checks, cleanup_check)
            return
        prepare(worker)
        files = worker.configuration.artifact.manifest["files"]
        preflight = probe(host, port, public_port, worker.probe_slot.hex, files)
        activate(worker, preflight)
        checks = probe(host, port, public_port, destination["target_slot"], files)
        cleanup_preflight(worker)
        cleanup_check = absent(host, port, public_port, worker.probe_slot.hex)
        finish(worker, checks, cleanup_check)
    except LeaseLost:
        return
    except Exception:
        try:
            if worker.status == "recovering":
                with locked(worker) as current:
                    owned(worker, current, "recovering")
                    current.status = (
                        "needs_attention" if current.recovery_attempts >= 3 else "recovery_pending"
                    )
                    current.deadline = timezone.now() + timedelta(seconds=30)
                    current.error = (
                        "Recovery could not verify the previous target state. "
                        "Check staging service health and retained files, then retry recovery."
                    )
                    current.save(update_fields=["status", "deadline", "error"])
            else:
                schedule_recovery(worker)
        except LeaseLost:
            pass


def recover():
    ids = list(
        ReleaseDeployment.objects.filter(
            status__in=["running", "recovering"], deadline__lt=timezone.now()
        ).values_list("pk", flat=True)[:20]
    )
    for pk in ids:
        with transaction.atomic():
            current = ReleaseDeployment.objects.select_for_update().get(pk=pk)
            if (
                current.status not in {"running", "recovering"}
                or current.deadline >= timezone.now()
            ):
                continue
            current.status = (
                "needs_attention" if current.recovery_attempts >= 3 else "rollback_queued"
            )
            current.error = (
                "Release worker stopped or its lease expired. "
                "The previous target state must be verified."
            )
            current.save(update_fields=["status", "error"])


def request_rollback(release, user_id, *, retry=False):
    with locked(release) as current:
        root = checked_intent(current)
        if retry:
            if current.status != "needs_attention":
                raise ValueError("Recovery is not awaiting intervention")
        else:
            if current.status in [
                "rollback_queued",
                "recovering",
                "recovery_pending",
                "rolled_back",
            ]:
                return current
            if current.status != "published" or release_files.pointer(
                root, current.target.slot.hex
            ) != candidate_pointer(current):
                raise ValueError("This release is no longer the active deployment")
        if (
            current.target.releases.exclude(pk=current.pk).filter(status__in=ACTIVE).exists()
            or current.target.rehearsals.filter(status__in=rollback_rehearsal.ACTIVE).exists()
        ):
            raise ValueError("Another operation for this target is pending")
        current.status, current.recovery_attempts = "rollback_queued", 0
        current.rollback_requested_by_id, current.rollback_requested_at = user_id, timezone.now()
        current.error = (
            "Rollback requested. The previous target state will be restored and checked."
        )
        current.save(
            update_fields=[
                "status",
                "recovery_attempts",
                "rollback_requested_by",
                "rollback_requested_at",
                "error",
            ]
        )
        return current


def origin(release):
    destination = release.identity["destination"]
    return f"http://{destination['target_slot']}.localhost:{destination['public_port']}"


def verify_published(release, root):
    checked_intent(release)
    artifact = verify_candidate(release, root)
    report = release.report
    if (
        digest(report) != release.report_digest
        or report.get("pointer") != candidate_pointer(release)
        or report.get("identity_digest") != release.identity_digest
        or report.get("authorization_digest") != release.authorization_digest
        or digest(release.authorization) != release.authorization_digest
        or report.get("checks") != expected_report(artifact.manifest["files"])
        or report.get("cleanup") != absent_report()
        or not release.finished_at
        or report.get("checked_at") != release.finished_at.isoformat()
        or not isinstance(report.get("worker_revision"), str)
        or not report["worker_revision"]
    ):
        raise ValueError("Published receipt changed")


def verify_restored(release, pointer):
    root = checked_intent(release)
    artifact = restored_artifact(release)
    report = release.recovery_report
    if (
        not artifact
        or digest(report) != release.recovery_digest
        or report.get("identity_digest") != release.identity_digest
        or report.get("pointer") != pointer
        or pointer not in (baseline_pointer(release), restored_pointer(release))
        or report.get("checks") != expected_report(artifact.manifest["files"])
        or report.get("cleanup") != absent_report()
        or not isinstance(report.get("worker_revision"), str)
        or not report["worker_revision"]
    ):
        raise ValueError("Recovery receipt changed")
    return root


def delivery(artifact):
    result = {
        "can_publish": False,
        "history": [],
        "status": "Review staging configuration to publish",
    }
    configuration = artifact.release_configurations.order_by("-id").first()
    if not configuration:
        return result
    result.update(
        configuration=configuration,
        history=list(configuration.target.releases.order_by("-id")[:10]),
    )
    try:
        root, _ = release_configuration.settings()
        current_pointer = release_files.pointer(root, configuration.target.slot.hex)
        pending = configuration.target.releases.filter(status__in=ACTIVE).first()
        result.update(pending=pending, status="No active release at this target")
        if current_pointer:
            published = configuration.target.releases.filter(
                status="published", bundle=current_pointer["release_id"]
            ).first()
            if published and current_pointer == candidate_pointer(published):
                verify_published(published, root)
                result.update(
                    active=published, url=origin(published), status="Published to local staging"
                )
            else:
                recovered = (
                    configuration.target.releases.filter(
                        status="rolled_back", recovery_report__pointer=current_pointer
                    )
                    .order_by("-id")
                    .first()
                )
                if recovered:
                    verify_restored(recovered, current_pointer)
                    result.update(
                        restored=recovered,
                        url=origin(recovered),
                        status="Previous release restored",
                    )
                else:
                    result["status"] = (
                        "Target contains a retained release; "
                        "review its configuration before replacement"
                    )
        if pending:
            result["status"] = (
                "Recovery needs attention"
                if pending.status == "needs_attention"
                else "Release operation in progress"
            )
        else:
            report = evidence(artifact)
            state = rollback_rehearsal.identity(configuration)
            result.update(can_publish=report["ready"], expected_target=digest(state["baseline"]))
    except ERRORS:
        result["can_publish"] = False
    return result
