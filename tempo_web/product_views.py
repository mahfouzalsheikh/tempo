from __future__ import annotations

import uuid

from asgiref.sync import async_to_sync
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import ensure_csrf_cookie

from tempo.errors import ConfigError, LeaseLostError
from tempo.intake import IntakeConflict
from tempo.product_execution import ROLES, enqueue_product, readiness
from tempo.run_snapshot import restore_snapshot, snapshot_digest
from tempo.runtime import get_orchestrator

from .intake_views import detail, error_message, page, signed_in
from .models import AgentRun, ProductBrief


def controller_for(product):
    service = get_orchestrator()
    for controller in getattr(service, "orchestrators", [service]):
        store = getattr(controller, "persistence", None)
        if store and store.project_id == product.project_id:
            return controller
    raise IntakeConflict("This project's workflow is not loaded by the running service.")


def setup(product):
    try:
        controller = controller_for(product)
        store = controller.persistence
        source_digest = snapshot_digest(store.execution_snapshot)
        _, config = restore_snapshot(store.execution_snapshot, source_digest)
        profiles = list(config.agents)
        return {
            "blocked": readiness(config),
            "configuration_digest": source_digest,
            "profiles": profiles,
            "roles": [
                {
                    "name": role,
                    "selected": next(
                        (name for name, profile in config.agents.items() if profile.role == role),
                        profiles[0],
                    ),
                }
                for role in ROLES
            ],
            "parallel_limit": config.workflow.max_parallel_nodes,
            "environment": config.project.environment,
            "repository": config.tracker.provider.get("repo", "Configured checkout hook"),
            "checks": [
                {"id": check.id, "name": check.name} for check in config.validation.required_checks
            ],
        }
    except (IntakeConflict, ConfigError) as exc:
        return {"blocked": [str(exc)]}


def launch(product, payload, user_id):
    if payload.get("mode") != "candidate":
        raise IntakeConflict("This execution path requires mode: candidate.")
    controller = controller_for(product)
    run = enqueue_product(
        controller.persistence,
        product.pk,
        expected_plan_id=payload["expected_plan_id"],
        expected_plan_digest=payload["expected_plan_digest"],
        expected_configuration_digest=payload["expected_configuration_digest"],
        bindings=payload["bindings"],
        parallelism=payload["parallelism"],
        user_id=user_id,
    )
    async_to_sync(controller.refresh)()
    return run


@ensure_csrf_cookie
def execute(request, brief_id):
    if response := signed_in(request):
        return response
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    product = get_object_or_404(ProductBrief, pk=brief_id)
    context = detail(product)
    info = setup(product)
    error = ""
    if request.method == "POST":
        try:
            launch(
                product,
                {
                    "mode": request.POST.get("mode"),
                    "expected_plan_id": int(request.POST.get("expected_plan_id", "0")),
                    "expected_plan_digest": request.POST.get("expected_plan_digest", ""),
                    "expected_configuration_digest": request.POST.get(
                        "expected_configuration_digest", ""
                    ),
                    "bindings": {role: request.POST.get(f"profile_{role}", "") for role in ROLES},
                    "parallelism": int(request.POST.get("parallelism", "1")),
                },
                request.user.pk,
            )
            return redirect(f"/ideas/{product.pk}/#execution")
        except (ValueError, ConfigError, IntegrityError, ObjectDoesNotExist) as exc:
            error = error_message(exc)
    response = page(request, "execute", setup=info, error=error, **context)
    if request.method == "POST":
        response.status_code = 409
    return response


def execution_setup(request, brief_id):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return JsonResponse(setup(get_object_or_404(ProductBrief, pk=brief_id)))


def control(request, brief_id, run_id, action):
    if response := signed_in(request):
        return response
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if action not in {"pause", "resume", "retry", "cancel", "unblock"}:
        return JsonResponse({"error": "unsupported_action"}, status=400)
    run = get_object_or_404(AgentRun, pk=run_id, execution_plan__brief_revision__brief_id=brief_id)
    product = run.execution_plan.brief_revision.brief
    try:
        success, message = async_to_sync(controller_for(product).control_run)(
            run.pk,
            action,
            {},
            user_id=request.user.pk,
            idempotency_key=f"product-control:{uuid.uuid4()}",
        )
        if not success:
            raise IntakeConflict(message)
    except (ValueError, ConfigError, LeaseLostError) as exc:
        return page(request, "error", error=error_message(exc), brief_id=brief_id)
    return redirect(f"/ideas/{brief_id}/#execution")


def run_details(plan):
    rows = []
    for run in plan.runs.order_by("-id"):
        candidate = run.checkpoints.filter(kind="product_candidate").order_by("-sequence").first()
        rows.append(
            {
                "id": run.pk,
                "status": run.status,
                "phase": run.phase,
                "error": run.error,
                "snapshot_digest": run.snapshot_digest,
                "workspace_path": run.workspace_path,
                "candidate": candidate.payload
                if (
                    candidate
                    and run.status == "succeeded"
                    and run.phase == "CandidateChecksPassed"
                    and candidate.payload.get("snapshot_digest") == run.snapshot_digest
                )
                else None,
                "nodes": list(
                    run.node_runs.values("node_key", "name", "status", "error", "attempt")
                ),
                "tokens": run.total_tokens,
                "active": run.status in {"running", "waiting_approval"},
                "resumable": run.status in {"failed", "cancelled", "paused"},
            }
        )
    return rows
