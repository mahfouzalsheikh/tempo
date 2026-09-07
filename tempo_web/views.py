from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth import authenticate
from django.contrib.staticfiles import finders
from django.db import transaction
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie

from tempo.runtime import get_orchestrator
from tempo_web.jwt_auth import clear_access_cookie, issue_access_token, set_access_cookie


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
        {
            "status": "ok" if orchestrator else "starting",
            "revision": os.getenv("TEMPO_BUILD_REVISION", "unknown"),
        },
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
    # Reconnect periodically to recheck session/user state, and never stream past JWT expiry.
    deadline = min(
        timezone.now().timestamp() + 300,
        getattr(request, "tempo_access_expires_at", float("inf")),
    )

    async def stream():
        try:
            initial = json.dumps(orchestrator.snapshot(), default=str, separators=(",", ":"))
            yield f"data:{initial}\n\n".encode()
            while timezone.now().timestamp() < deadline:
                try:
                    await asyncio.wait_for(
                        queue.get(), timeout=min(15, max(0, deadline - timezone.now().timestamp())),
                    )
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
    response["Cache-Control"] = "no-store, no-transform"
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


def _safe_next_url(request: HttpRequest, value: object) -> str:
    candidate = str(value or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse("dashboard")


@ensure_csrf_cookie
def login_page(request: HttpRequest) -> HttpResponse:
    next_url = _safe_next_url(request, request.GET.get("next"))
    if request.user.is_authenticated:
        return redirect(next_url)
    response = render(request, "login.html", {"next_url": next_url})
    response["Cache-Control"] = "no-store"
    return response


def logout_page(request: HttpRequest) -> HttpResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    response = redirect("login")
    clear_access_cookie(response)
    response["Cache-Control"] = "no-store"
    return response


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


def auth_login(request: HttpRequest) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({"error": "invalid_request", "message": str(exc)}, status=400)
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or not password:
        return JsonResponse(
            {"error": "credentials_required", "message": "Username and password are required."},
            status=400,
        )
    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse(
            {"error": "invalid_credentials", "message": "Invalid username or password."},
            status=401,
        )
    token, expires_at = issue_access_token(user)
    response = JsonResponse(
        {
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": expires_at.isoformat(),
            "user": {"id": user.pk, "username": user.get_username()},
            "redirect": _safe_next_url(request, payload.get("next")),
        }
    )
    set_access_cookie(response, token, expires_at)
    response["Cache-Control"] = "no-store"
    return response


def auth_logout(request: HttpRequest) -> JsonResponse | HttpResponseNotAllowed:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    response = JsonResponse({"status": "signed_out"})
    clear_access_cookie(response)
    response["Cache-Control"] = "no-store"
    return response


def auth_me(request: HttpRequest) -> JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    return JsonResponse(
        {
            "user": {
                "id": request.user.pk,
                "username": request.user.get_username(),
                "is_staff": request.user.is_staff,
            },
            "authentication": (
                "jwt" if getattr(request, "tempo_jwt_authenticated", False) else "session"
            ),
        }
    )


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
            or (action == "restart" and existing_action.payload.get("expected_snapshot_digest")
                != payload.get("expected_snapshot_digest"))
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
    from django.db import IntegrityError

    from tempo.errors import ConfigError, LeaseLostError

    try:
        success, message = async_to_sync(orchestrator.control_run)(
            run_id, action, payload, user_id=request.user.pk, idempotency_key=idempotency_key,
        )
    except (LeaseLostError, ConfigError):
        return JsonResponse(
            {"error": "run_or_configuration_changed_refresh_before_retry"}, status=409,
        )
    except IntegrityError:
        return JsonResponse({"error": "conflicting_control_request"}, status=409)
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


@transaction.atomic
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
    from .models import AgentRun

    run = AgentRun.objects.select_for_update().get(pk=approval.run_id)
    approval.refresh_from_db()
    if AgentRun.objects.filter(restarted_from=run).exists():
        return JsonResponse({"error": "run_superseded"}, status=409)
    if approval.status != ApprovalRequest.Status.PENDING:
        return JsonResponse({"error": "pending_approval_not_found"}, status=404)
    approval.run = run
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

    orchestrator = get_orchestrator()
    controllers = getattr(orchestrator, "orchestrators", [orchestrator])
    current_digests = {}
    from tempo.run_snapshot import snapshot_digest

    for controller in controllers:
        persistence = getattr(controller, "persistence", None)
        if persistence and persistence.execution_snapshot:
            current_digests[persistence.project_id] = snapshot_digest(
                persistence.execution_snapshot,
            )

    rows = (
        AgentRun.objects.filter(
            Q(
                status__in=[
                    AgentRun.Status.PAUSED,
                    AgentRun.Status.WAITING_APPROVAL,
                    AgentRun.Status.FAILED,
                    AgentRun.Status.CANCELLED,
                ]
            )
            | Q(phase="SafetyLimitReached")
            | Q(snapshot_digest="", status__in=["queued", "retry_scheduled"])
        )
        .filter(successor__isnull=True)
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
                    "snapshot_digest": row.snapshot_digest,
                    "restarted_from_id": row.restarted_from_id,
                    "restart_snapshot_digest": current_digests.get(row.project_id, ""),
                    "can_restart": row.status in {
                        "failed", "cancelled", "paused", "queued", "retry_scheduled",
                    } and not row.lease_token and not row.worker_id
                    and row.project_id in current_digests,
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
