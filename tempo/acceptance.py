"""Immutable reviewed checks, durable dispatch, and fenced browser evidence."""

import asyncio
import os
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from tempo.acceptance_contract import digest, specification, verify_report, verify_specification
from tempo.acceptance_runner import TIMEOUT, CleanupError, execute
from tempo.validation_sandbox import remove_container
from tempo_web.artifact_views import verified_candidate_artifact
from tempo_web.models import AcceptanceAttempt, AcceptanceSuite, BuildArtifact


def configured_image():
    value = os.getenv("TEMPO_ACCEPTANCE_IMAGE", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("The browser acceptance worker is not configured.")
    return value


def identity(artifact, suite, image):
    return {
        "artifact_digest": artifact.digest,
        "manifest_digest": artifact.manifest_digest,
        "source_sha": artifact.manifest["source_sha"],
        "snapshot_digest": artifact.run.snapshot_digest,
        "suite_digest": suite.digest,
        "brief_digest": artifact.manifest["brief_digest"],
        "image": image,
    }


def checked_suite(suite):
    context = suite.artifact.run.product_snapshot
    return verify_specification(
        suite.specification, suite.digest, digest(context["brief"]), context["brief"]["criteria"]
    )


@transaction.atomic
def approve(artifact_id, instructions, expected_suite_id, user_id):
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    verified_candidate_artifact(artifact)
    latest = artifact.acceptance_suites.order_by("-id").first()
    if (latest.pk if latest else 0) != expected_suite_id:
        raise ValueError("The check plan changed. Reload before approving a new version.")
    context = artifact.run.product_snapshot
    saved = specification(digest(context["brief"]), context["brief"]["criteria"], instructions)
    if latest and latest.specification == saved:
        return latest
    return AcceptanceSuite.objects.create(
        artifact=artifact, specification=saved, digest=digest(saved), approved_by_id=user_id
    )


@transaction.atomic
def enqueue(artifact_id, suite_id, suite_digest, request_key, user_id):
    artifact = BuildArtifact.objects.select_for_update().get(pk=artifact_id)
    suite = artifact.acceptance_suites.order_by("-id").first()
    if not suite or suite.pk != suite_id or suite.digest != suite_digest:
        raise ValueError("The reviewed check plan changed. Reload before starting checks.")
    verified_candidate_artifact(artifact)
    checked_suite(suite)
    existing = AcceptanceAttempt.objects.filter(request_key=request_key).first()
    if existing:
        if existing.suite_id != suite.pk or existing.requested_by_id != user_id:
            raise ValueError("This request key belongs to another check run.")
        return existing
    active = AcceptanceAttempt.objects.filter(
        suite__artifact=artifact, status__in=["queued", "running"]
    ).first()
    if active:
        if active.suite_id != suite.pk:
            raise ValueError("An earlier check plan is still running. Refresh after it finishes.")
        return active
    image = configured_image()
    return AcceptanceAttempt.objects.create(
        suite=suite,
        request_key=request_key,
        requested_by_id=user_id,
        image=image,
        identity=identity(artifact, suite, image),
    )


@transaction.atomic
def claim():
    now = timezone.now()
    attempt = (
        AcceptanceAttempt.objects.select_for_update(skip_locked=True)
        .filter(
            status="queued",
        )
        .order_by("id")
        .first()
    )
    if attempt:
        attempt.status, attempt.started_at = "running", now
        attempt.deadline, attempt.lease_token = now + timedelta(seconds=TIMEOUT + 60), uuid.uuid4()
        attempt.save(update_fields=["status", "started_at", "deadline", "lease_token"])
    return attempt


@transaction.atomic
def finish(attempt, report=None, error=""):
    current = AcceptanceAttempt.objects.select_for_update().get(pk=attempt.pk)
    if (
        current.status != "running"
        or current.lease_token != attempt.lease_token
        or current.deadline <= timezone.now()
    ):
        return False
    if report is not None:
        verified_candidate_artifact(current.suite.artifact)
        if identity(current.suite.artifact, current.suite, current.image) != current.identity:
            raise ValueError("Acceptance evidence identity changed")
        passed = verify_report(
            report, checked_suite(current.suite), current.identity["artifact_digest"]
        )
        current.status = "passed" if passed else "failed"
        current.report, current.report_digest = report, digest(report)
    else:
        current.status = "interrupted"
    current.error, current.finished_at = error[:2000], timezone.now()
    current.save(update_fields=["status", "report", "report_digest", "error", "finished_at"])
    return True


def process(attempt):
    import json

    root = Path(os.environ["TEMPO_WORKSPACE_ROOT"]).resolve() / ".acceptance"
    directory = root / attempt.request_key.hex
    try:
        artifact, suite = attempt.suite.artifact, attempt.suite
        data = verified_candidate_artifact(artifact)
        if identity(artifact, suite, attempt.image) != attempt.identity:
            raise ValueError("The saved build or reviewed checks changed")
        spec = checked_suite(suite)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        (directory / "artifact.zip").write_bytes(data)
        (directory / "checks.json").write_text(
            json.dumps(
                {
                    "suite": spec,
                    "suite_digest": suite.digest,
                    "artifact_digest": artifact.digest,
                    "files": artifact.manifest["files"],
                    "brief_digest": spec["brief_digest"],
                    "criteria": artifact.run.product_snapshot["brief"]["criteria"],
                }
            )
        )
        report = asyncio.run(execute(attempt.image, directory, attempt.request_key))
        # Cleanup is a prerequisite to authoritative success.
        shutil.rmtree(directory)
        finish(attempt, report=report)
    except CleanupError as error:
        AcceptanceAttempt.objects.filter(
            pk=attempt.pk, status="running", lease_token=attempt.lease_token
        ).update(
            deadline=timezone.now(),
            error=str(error),
        )
    except Exception as error:
        finish(
            attempt,
            error=str(error) or "Browser execution stopped before producing complete evidence.",
        )
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def recover():
    """Clear expired work only after its isolated process and private inputs are gone."""
    root = Path(os.environ["TEMPO_WORKSPACE_ROOT"]).resolve() / ".acceptance"
    for attempt in AcceptanceAttempt.objects.filter(
        status="running",
        deadline__lt=timezone.now(),
    ).order_by("id")[:10]:
        try:
            asyncio.run(remove_container(f"tempo-acceptance-{attempt.request_key.hex}"))
            directory = root / attempt.request_key.hex
            if directory.exists():
                shutil.rmtree(directory)
        except Exception:
            continue  # Keep dispatch blocked until cleanup can be confirmed.
        AcceptanceAttempt.objects.filter(
            pk=attempt.pk, status="running", lease_token=attempt.lease_token
        ).update(
            status="interrupted",
            finished_at=timezone.now(),
            error="Worker interrupted or deadline expired. Start a fresh check run.",
        )
