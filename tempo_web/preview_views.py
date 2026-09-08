"""Operator-only preview lifecycle; generated HTML is never served by Django."""

import hashlib
import os
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from tempo import preview_files
from tempo.errors import CodexError, ConfigError

from .artifact_views import verified_candidate_artifact
from .intake_views import page, signed_in
from .models import BuildArtifact, PreviewDeployment


def configuration():
    root = os.getenv("TEMPO_PREVIEW_ROOT", "")
    port = int(os.getenv("TEMPO_PREVIEW_PORT", "8031"))
    if not root or not Path(root).is_absolute() or not 1024 <= port <= 65535:
        raise ValueError("Local preview hosting is not configured")
    return Path(root), port


def details(artifact_id):
    try:
        root, port = configuration()
    except ValueError:
        return {"configured": False}
    deployment = PreviewDeployment.objects.filter(artifact_id=artifact_id).first()
    result = {"configured": True}
    if deployment:
        live = deployment.active and preview_files.available(
            root,
            deployment.token.hex,
            deployment.artifact.digest,
        )
        result.update(
            active=live,
            expires_at=deployment.expires_at,
            url=f"http://{deployment.token.hex}.localhost:{port}/" if live else "",
        )
    return result


def control(request, brief_id, run_id, artifact_id, action):
    if response := signed_in(request):
        return response
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if action not in {"start", "stop"}:
        return JsonResponse({"error": "Unknown preview action."}, status=404)
    try:
        root, _port = configuration()
        with transaction.atomic():
            # Serialize launches, expiration renewals, repairs, and revocation per artifact.
            artifact = get_object_or_404(
                BuildArtifact.objects.select_for_update(),
                pk=artifact_id,
                run_id=run_id,
                run__execution_plan__brief_revision__brief_id=brief_id,
            )
            deployment = PreviewDeployment.objects.filter(artifact=artifact).first()
            if action == "stop":
                if deployment:
                    preview_files.remove(root, deployment.token.hex)
                    deployment.active = False
                    deployment.save(update_fields=["active", "updated_at"])
            else:
                data = verified_candidate_artifact(artifact)
                intact = False
                if (
                    deployment
                    and deployment.active
                    and preview_files.available(
                        root,
                        deployment.token.hex,
                        artifact.digest,
                    )
                ):
                    stored = (root / deployment.token.hex / "artifact.zip").read_bytes()
                    intact = hashlib.sha256(stored).hexdigest() == artifact.digest
                if not intact:
                    if deployment:
                        preview_files.remove(root, deployment.token.hex)
                    token = uuid.uuid4()
                    expires_at = timezone.now() + timedelta(hours=24)
                    preview_files.publish(
                        root,
                        token.hex,
                        data,
                        artifact.manifest["files"],
                        artifact.digest,
                        expires_at.timestamp(),
                    )
                    PreviewDeployment.objects.update_or_create(
                        artifact=artifact,
                        defaults={
                            "token": token,
                            "active": True,
                            "expires_at": expires_at,
                            "created_by": request.user,
                        },
                    )
    except (CodexError, ConfigError, ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile):
        response = page(
            request,
            "error",
            brief_id=brief_id,
            error="Tempo could not update this preview. Check that the build and preview "
            "service are available, then return to the idea and try again.",
        )
        response.status_code = 409
        return response
    return redirect(f"/ideas/{brief_id}/#execution")
