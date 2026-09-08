"""Current release assessment, separate from immutable historical build evidence."""

import uuid

from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from tempo import preview_health, release_configuration, releases, rollback_rehearsal
from tempo.acceptance_contract import digest
from tempo.errors import CodexError, ConfigError

from .acceptance_views import summary
from .artifact_views import verified_candidate_artifact
from .intake_views import signed_in
from .models import BuildArtifact, PreviewDeployment, PreviewHealthAttempt, RollbackRehearsal
from .preview_views import details as preview_details


def evaluate(artifact):
    gates = []

    def gate(key, title, passed, detail):
        gates.append(
            {
                "id": key,
                "title": title,
                "status": "passed" if passed else "blocked",
                "detail": detail,
            }
        )

    valid = True
    try:
        verified_candidate_artifact(artifact)
    except (ValueError, KeyError, TypeError, CodexError, ConfigError):
        valid = False
    gate(
        "candidate",
        "Build and required checks",
        valid,
        "Saved ZIP, source identity, approved execution contract, and required checks verified."
        if valid
        else "Build integrity or required candidate evidence is unavailable.",
    )

    plan = artifact.run.execution_plan
    revision = plan.brief_revision if plan else None
    latest_revision = revision.brief.revisions.first() if revision else None
    latest_plan = latest_revision.plans.first() if latest_revision else None
    current = bool(
        valid
        and latest_plan
        and latest_plan.pk == plan.pk
        and plan.approved_at
        and plan.approved_by_id
        and digest(plan.specification)
        == plan.digest
        == digest(artifact.run.product_snapshot["plan"])
        and digest(revision.specification)
        == revision.digest
        == digest(artifact.run.product_snapshot["brief"])
    )
    gate(
        "scope",
        "Current approved scope",
        current,
        "This build uses the latest brief and approved plan."
        if current
        else "Review the latest brief and plan, approve it, and build that version.",
    )

    acceptance = summary(artifact)
    passed = valid and bool(acceptance.get("passed"))
    gate(
        "acceptance",
        "Reviewed browser checks",
        passed,
        "Every criterion in the latest reviewed check plan passed for this exact build."
        if passed
        else f"{acceptance['status'].capitalize()}. Open acceptance checks to continue.",
    )
    health = preview_health.summary(artifact)
    gate(
        "preview_health",
        "Preview health evidence",
        valid and health["passed"],
        "The root page and every saved file matched over HTTP, with the expected browser "
        "security headers. Evidence expires after five minutes."
        if valid and health["passed"]
        else health["status"].capitalize(),
    )
    configuration = release_configuration.summary(artifact)
    gate(
        "target",
        "Deployment target and configuration",
        valid and configuration["passed"],
        configuration["status"],
    )
    rehearsal = rollback_rehearsal.summary(artifact)
    gate(
        "rollback",
        "Rollback rehearsal",
        valid and rehearsal["passed"],
        rehearsal["status"],
    )
    suite, attempt = acceptance.get("suite"), acceptance.get("attempt")
    return {
        "schema": 4,
        "evaluator": "release-readiness-v4",
        "evaluated_at": timezone.now().isoformat(),
        "ready": all(item["status"] == "passed" for item in gates),
        "artifact_id": artifact.pk,
        "artifact_digest": artifact.digest,
        "manifest_digest": artifact.manifest_digest,
        "source_sha": artifact.manifest.get("source_sha") if valid else None,
        "snapshot_digest": artifact.run.snapshot_digest,
        "plan_id": plan.pk if plan else None,
        "latest_plan_id": latest_plan.pk if latest_plan else None,
        "rollback_rehearsal": {
            "passed": valid and rehearsal["passed"],
            "status": rehearsal["status"],
            "attempt_id": rehearsal["attempt"].pk if rehearsal["attempt"] else None,
            "report_digest": rehearsal["attempt"].report_digest if rehearsal["passed"] else None,
            "report": rehearsal.get("report") if valid else None,
            "valid_until": rehearsal["valid_until"].isoformat()
            if rehearsal.get("valid_until")
            else None,
        },
        "release_configuration": {
            "status": configuration["status"],
            "passed": valid and configuration["passed"],
            "id": configuration["configuration"].pk if configuration["configuration"] else None,
            "digest": configuration["configuration"].digest if configuration["passed"] else None,
            "specification": configuration["configuration"].specification
            if configuration["passed"]
            else None,
        },
        "preview_health": {
            "status": health["status"],
            "passed": valid and health["passed"],
            "attempt_id": health["attempt"].pk if health["attempt"] else None,
            "valid_until": health["valid_until"].isoformat() if health.get("valid_until") else None,
            "report_digest": health["attempt"].report_digest if health["passed"] else None,
            "report": health.get("report") if valid else None,
        },
        "acceptance": {
            "suite_id": suite.pk if suite else None,
            "suite_digest": suite.digest if suite else None,
            "attempt_id": attempt.pk if attempt else None,
            "status": acceptance["status"],
            "report_digest": attempt.report_digest if passed else None,
            "image": attempt.image if passed else None,
            "results": acceptance.get("results", []) if valid else [],
        },
        "gates": gates,
        "notice": "This is a point-in-time assessment, not deployment authorization. "
        "Promotion must re-evaluate current evidence. The original build manifest "
        "remains unchanged.",
    }


def readiness(request, brief_id, run_id, artifact_id):
    if response := signed_in(request):
        return response
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    artifact = get_object_or_404(
        BuildArtifact,
        pk=artifact_id,
        run_id=run_id,
        run__execution_plan__brief_revision__brief_id=brief_id,
    )
    report = evaluate(artifact)
    if request.GET.get("format") == "json":
        response = JsonResponse({"assessment": report, "digest": digest(report)})
        response["Content-Disposition"] = (
            f'attachment; filename="tempo-readiness-{artifact.pk}.json"'
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response
    try:
        preview = preview_details(artifact.pk)
    except (OSError, ValueError, KeyError, TypeError):
        preview = {"configured": False}
    return render(
        request,
        "release.html",
        {
            "brief_id": brief_id,
            "artifact": artifact,
            "report": report,
            "delivery": releases.delivery(artifact),
            "preview": preview,
            "health": preview_health.summary(artifact),
            "rehearsal": rollback_rehearsal.summary(artifact),
            "rehearsal_history": RollbackRehearsal.objects.filter(
                configuration__artifact=artifact
            ).order_by("-id")[:10],
            "request_key": uuid.uuid4(),
            "health_history": PreviewHealthAttempt.objects.filter(
                deployment__artifact=artifact
            ).order_by("-id")[:10],
            "passed_count": sum(gate["status"] == "passed" for gate in report["gates"]),
            "blocked_count": sum(gate["status"] != "passed" for gate in report["gates"]),
        },
    )


def check_preview(request, brief_id, run_id, artifact_id):
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
        preview_health.enqueue(
            artifact.pk,
            request.POST.get("generation", ""),
            uuid.UUID(request.POST.get("request_key", "")),
            request.user.pk,
        )
    except (ValueError, KeyError, TypeError, OSError, CodexError, ConfigError):
        return JsonResponse(
            {
                "error": "Preview check could not start. Refresh readiness, then "
                "start or renew the preview and try again."
            },
            status=409,
        )
    except PreviewDeployment.DoesNotExist:
        return JsonResponse({"error": "Start a preview before checking its health."}, status=409)
    return redirect("release_readiness", brief_id=brief_id, run_id=run_id, artifact_id=artifact_id)


def rehearse_rollback(request, brief_id, run_id, artifact_id):
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
        rollback_rehearsal.enqueue(
            artifact.pk,
            int(request.POST.get("configuration_id", "0")),
            request.POST.get("configuration_digest", ""),
            uuid.UUID(request.POST.get("request_key", "")),
            request.user.pk,
        )
    except rollback_rehearsal.ERRORS:
        from django.contrib import messages

        messages.error(
            request,
            "Rollback rehearsal could not start. "
            "Refresh the configuration and target, then try again.",
        )
    return redirect("release_readiness", brief_id=brief_id, run_id=run_id, artifact_id=artifact_id)
