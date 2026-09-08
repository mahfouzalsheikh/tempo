from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404

from tempo.build_artifacts import verify_artifact
from tempo.errors import CodexError, ConfigError
from tempo.product_execution import restore_product

from .intake_views import signed_in
from .models import BuildArtifact, ValidationAttempt


def download(request, brief_id, run_id, artifact_id, kind):
    if response := signed_in(request):
        return response
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    artifact = get_object_or_404(
        BuildArtifact.objects.select_related("run"),
        pk=artifact_id,
        run_id=run_id,
        run__execution_plan__brief_revision__brief_id=brief_id,
        run__status="succeeded",
    )
    try:
        data = verified_candidate_artifact(artifact)
    except (CodexError, ConfigError, ValueError, KeyError):
        return JsonResponse(
            {"error": "Build artifact integrity or evidence check failed."}, status=409
        )
    if kind == "manifest":
        response = JsonResponse(artifact.manifest)
        extension = "json"
    elif kind == "archive":
        response = HttpResponse(data, content_type="application/zip")
        extension = "zip"
    else:
        return JsonResponse({"error": "Unknown artifact format."}, status=404)
    response["Content-Disposition"] = f'attachment; filename="tempo-{artifact.digest}.{extension}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cache-Control"] = "private, no-store"
    return response


def verified_candidate_artifact(artifact):
    """Shared download/deployment gate; callers must authenticate and scope the artifact."""
    run = artifact.run
    if run.status != "succeeded" or run.phase != "CandidateChecksPassed":
        raise ValueError("Candidate has not passed checks")
    checkpoint = run.checkpoints.filter(
        kind="product_candidate", payload__artifact__id=artifact.pk
    ).first()
    context = restore_product(run)
    data = verify_artifact(artifact)
    expected = {
        "id": artifact.pk,
        "sha256": artifact.digest,
        "size": artifact.size,
        "manifest_digest": artifact.manifest_digest,
    }
    if not checkpoint or checkpoint.payload.get("artifact") != expected:
        raise ValueError("Missing candidate evidence")
    candidate = checkpoint.payload
    for key in (
        "source_sha",
        "snapshot_digest",
        "brief_digest",
        "plan_digest",
        "plan_id",
        "validation_record_id",
        "policy_digest",
        "required_check_ids",
        "workspace_fingerprint",
    ):
        if artifact.manifest.get(key) != candidate.get(key):
            raise ValueError("Candidate evidence mismatch")
    if candidate["snapshot_digest"] != run.snapshot_digest or artifact.manifest.get(
        "build_profile"
    ) != context.get("build_profile"):
        raise ValueError("Build contract mismatch")
    if not ValidationAttempt.objects.filter(
        pk=candidate["validation_record_id"],
        run=run,
        status="passed",
        policy_digest=candidate["policy_digest"],
        workspace_fingerprint=candidate["workspace_fingerprint"],
        required_check_ids=candidate["required_check_ids"],
    ).exists():
        raise ValueError("Validation evidence missing")
    return data
