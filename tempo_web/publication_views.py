import uuid

from django.contrib import messages
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect

from tempo import releases
from tempo.acceptance_contract import digest

from .intake_views import signed_in
from .models import BuildArtifact, ReleaseDeployment


def publish(request, brief_id, run_id, artifact_id):
    if response := signed_in(request):
        return response
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    artifact = get_object_or_404(
        BuildArtifact,
        pk=artifact_id,
        run_id=run_id,
        run__execution_plan__brief_revision__brief_id=brief_id,
    )
    try:
        if request.POST.get("reviewed") != "on":
            raise ValueError("Review the destination before publishing")
        releases.enqueue(
            artifact.pk,
            int(request.POST.get("configuration_id", "0")),
            request.POST.get("configuration_digest", ""),
            request.POST.get("expected_target", ""),
            uuid.UUID(request.POST.get("request_key", "")),
            request.user.pk,
        )
    except releases.ERRORS:
        messages.error(
            request,
            "Publication could not start. "
            "Review the current gates and destination, then try again.",
        )
    return redirect("release_readiness", brief_id=brief_id, run_id=run_id, artifact_id=artifact_id)


def control(request, brief_id, run_id, artifact_id, release_id, action):
    if response := signed_in(request):
        return response
    release = get_object_or_404(
        ReleaseDeployment,
        pk=release_id,
        configuration__artifact_id=artifact_id,
        configuration__artifact__run_id=run_id,
        configuration__artifact__run__execution_plan__brief_revision__brief_id=brief_id,
    )
    if action == "evidence":
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        document = {
            "schema": 1,
            "release_id": release.pk,
            "status": release.status,
            "requested_at": release.created_at.isoformat(),
            "requested_by_id": release.requested_by_id,
            "identity": release.identity,
            "identity_digest": release.identity_digest,
            "authorization": release.authorization,
            "authorization_digest": release.authorization_digest,
            "report": release.report,
            "report_digest": release.report_digest,
            "recovery_report": release.recovery_report,
            "recovery_digest": release.recovery_digest,
            "rollback_requested_at": release.rollback_requested_at.isoformat()
            if release.rollback_requested_at
            else None,
            "rollback_requested_by_id": release.rollback_requested_by_id,
            "error": release.error,
        }
        response = JsonResponse({"release": document, "digest": digest(document)})
        response["Content-Disposition"] = f'attachment; filename="tempo-release-{release.pk}.json"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if action not in {"rollback", "retry"}:
        return JsonResponse({"error": "Unknown release action"}, status=404)
    try:
        releases.request_rollback(release, request.user.pk, retry=action == "retry")
    except releases.ERRORS:
        messages.error(
            request,
            "Release recovery could not start. "
            "Refresh the target and operation history, then try again.",
        )
    return redirect("release_readiness", brief_id=brief_id, run_id=run_id, artifact_id=artifact_id)
