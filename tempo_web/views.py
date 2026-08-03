from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.staticfiles import finders
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from tempo.runtime import get_orchestrator


async def static_asset(request: HttpRequest, name: str) -> HttpResponse:
    found = await asyncio.to_thread(finders.find, name)
    if not found:
        raise Http404
    path = Path(found)
    content = await asyncio.to_thread(path.read_bytes)
    response = HttpResponse(
        content, content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    response["Cache-Control"] = "no-cache"
    return response


def health(request: HttpRequest) -> JsonResponse:
    orchestrator = get_orchestrator()
    return JsonResponse(
        {"status": "ok" if orchestrator else "starting"},
        status=200 if orchestrator else 503,
    )


def state(request: HttpRequest) -> JsonResponse:
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    return JsonResponse(orchestrator.snapshot())


async def state_events(
    request: HttpRequest,
) -> StreamingHttpResponse | HttpResponseNotAllowed | JsonResponse:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    queue = orchestrator.subscribe_events()

    async def stream():
        try:
            initial = json.dumps(orchestrator.snapshot(), default=str, separators=(",", ":"))
            yield f"data:{initial}\n\n".encode()
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield b":keepalive\n\n"
                    continue
                payload = json.dumps(
                    orchestrator.snapshot(),
                    default=str,
                    separators=(",", ":"),
                )
                yield f"data:{payload}\n\n".encode()
        finally:
            orchestrator.unsubscribe_events(queue)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


def admin_state(request: HttpRequest) -> JsonResponse:
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    return JsonResponse(orchestrator.admin_snapshot())


def issue(request: HttpRequest, identifier: str) -> JsonResponse:
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    row = orchestrator.issue_snapshot(identifier)
    if not row:
        return JsonResponse({"error": "issue_not_found", "identifier": identifier}, status=404)
    return JsonResponse(row)


def refresh(request: HttpRequest) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    async_to_sync(orchestrator.refresh)()
    return JsonResponse({"status": "refresh_scheduled"}, status=202)


def _json_payload(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        value = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def run_action(
    request: HttpRequest,
    run_id: int,
    action: str,
) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({"error": "invalid_request", "message": str(exc)}, status=400)
    idempotency_key = (
        request.headers.get("Idempotency-Key", "").strip()
        or f"operator:{request.user.pk}:{run_id}:{action}:{uuid.uuid4().hex}"
    )
    from .models import OperatorAction

    existing_action = OperatorAction.objects.filter(idempotency_key=idempotency_key).first()
    if existing_action:
        if (
            existing_action.run_id != run_id
            or existing_action.action != action
            or existing_action.requested_by_id != request.user.pk
        ):
            return JsonResponse({"error": "idempotency_key_conflict"}, status=409)
        return JsonResponse(
            {
                "status": existing_action.status,
                "run_id": run_id,
                "action": action,
                "message": existing_action.message,
                "idempotency_key": idempotency_key,
            }
        )
    success, message = async_to_sync(orchestrator.control_run)(
        run_id,
        action,
        payload,
        user_id=request.user.pk,
        idempotency_key=idempotency_key,
    )
    if not success:
        status = 404 if message == "run_not_found" else 409
        return JsonResponse({"error": message}, status=status)
    return JsonResponse(
        {
            "status": "applied",
            "run_id": run_id,
            "action": action,
            "message": message,
            "idempotency_key": idempotency_key,
        },
        status=202,
    )


def approvals(request: HttpRequest) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    from .models import ApprovalRequest

    rows = ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).select_related(
        "run",
        "run__issue",
        "run__project",
    )
    return JsonResponse(
        {
            "approvals": [
                {
                    "id": row.pk,
                    "run_id": row.run_id,
                    "issue": row.run.issue.identifier,
                    "project": str(row.run.project) if row.run.project else None,
                    "kind": row.kind,
                    "title": row.title,
                    "details": row.details,
                    "proposed_arguments": row.proposed_arguments,
                    "requested_at": row.requested_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


def approval_decision(
    request: HttpRequest,
    approval_id: int,
) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    from .models import ApprovalRequest

    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({"error": "invalid_request", "message": str(exc)}, status=400)
    decision = str(payload.get("decision", "")).strip().lower()
    if decision not in {"approve", "reject"}:
        return JsonResponse(
            {"error": "decision_must_be_approve_or_reject"},
            status=400,
        )
    approval = ApprovalRequest.objects.filter(
        pk=approval_id,
        status=ApprovalRequest.Status.PENDING,
    ).first()
    if not approval:
        return JsonResponse({"error": "pending_approval_not_found"}, status=404)
    edited_arguments = payload.get("arguments", {})
    if edited_arguments is not None and not isinstance(edited_arguments, dict):
        return JsonResponse({"error": "arguments_must_be_an_object"}, status=400)
    approval.status = (
        ApprovalRequest.Status.APPROVED
        if decision == "approve"
        else ApprovalRequest.Status.REJECTED
    )
    approval.edited_arguments = edited_arguments or {}
    approval.decision_note = str(payload.get("note", "")).strip()
    approval.decided_by = request.user
    approval.decided_at = timezone.now()
    approval.save(
        update_fields=[
            "status",
            "edited_arguments",
            "decision_note",
            "decided_by",
            "decided_at",
        ]
    )
    if approval.run.lease_expires_at is None or approval.run.lease_expires_at <= timezone.now():
        from .models import AgentRun

        approval.run.status = AgentRun.Status.RETRY_SCHEDULED
        approval.run.phase = "ApprovalDecided"
        approval.run.available_at = timezone.now()
        approval.run.save(update_fields=["status", "phase", "available_at"])
        orchestrator = get_orchestrator()
        if orchestrator:
            async_to_sync(orchestrator.refresh)()
    return JsonResponse({"status": approval.status, "approval_id": approval.pk})


def control_state(request: HttpRequest) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    from django.db.models import Q

    from .models import AgentRun

    rows = (
        AgentRun.objects.filter(
            Q(
                status__in=[
                    AgentRun.Status.PAUSED,
                    AgentRun.Status.WAITING_APPROVAL,
                ]
            )
            | Q(phase="SafetyLimitReached")
        )
        .select_related("issue", "project", "project__organization")
        .order_by("priority", "-started_at")[:100]
    )
    return JsonResponse(
        {
            "runs": [
                {
                    "run_id": row.pk,
                    "project": str(row.project) if row.project else None,
                    "identifier": row.issue.identifier,
                    "title": row.issue.title,
                    "status": row.status,
                    "phase": row.phase,
                    "priority": row.priority,
                    "attempt": row.attempt,
                    "error": row.error,
                    "started_at": row.started_at.isoformat(),
                    "checkpoint": row.checkpoint,
                }
                for row in rows
            ]
        }
    )


def platform_configuration(
    request: HttpRequest,
) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    return JsonResponse(orchestrator.platform_snapshot())


def update_platform_configuration(
    request: HttpRequest,
    organization: str,
    project: str,
) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({"error": "invalid_request", "message": str(exc)}, status=400)
    from tempo.errors import ConfigError

    from .models import PlatformConfigurationChange, Project, WorkflowVersion

    project_row = (
        Project.objects.select_related("organization")
        .filter(organization__slug=organization, slug=project)
        .first()
    )
    if not project_row:
        return JsonResponse({"error": "project_not_found"}, status=404)
    workflow_version = (
        WorkflowVersion.objects.filter(project=project_row, active=True)
        .order_by("-version")
        .first()
    )
    try:
        updated = async_to_sync(orchestrator.update_platform_config)(
            organization,
            project,
            payload,
        )
        if not updated:
            return JsonResponse({"error": "project_not_found"}, status=404)
    except ConfigError as exc:
        PlatformConfigurationChange.objects.create(
            project=project_row,
            workflow_version=workflow_version,
            sections=payload,
            requested_by=request.user,
            status=PlatformConfigurationChange.Status.REJECTED,
            message=str(exc),
        )
        return JsonResponse(
            {"error": "invalid_platform_configuration", "message": str(exc)}, status=400
        )
    PlatformConfigurationChange.objects.create(
        project=project_row,
        workflow_version=workflow_version,
        sections=payload,
        requested_by=request.user,
        status=PlatformConfigurationChange.Status.APPLIED,
        message="Managed platform configuration stored; reload scheduled.",
    )
    return JsonResponse(
        {
            "status": "applied",
            "project": f"{organization}/{project}",
            "message": "Configuration validated, stored durably, and scheduled for reload.",
        },
        status=202,
    )


@ensure_csrf_cookie
def dashboard(request: HttpRequest) -> HttpResponse:
    orchestrator = get_orchestrator()
    snapshot = orchestrator.snapshot() if orchestrator else None
    return render(request, "dashboard.html", {"snapshot": snapshot})


@ensure_csrf_cookie
def admin_runtime(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_runtime.html")


@ensure_csrf_cookie
def admin_configuration(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_configuration.html")
