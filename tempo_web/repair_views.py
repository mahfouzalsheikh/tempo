import uuid

from asgiref.sync import async_to_sync
from django.db import IntegrityError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import ensure_csrf_cookie

from tempo.errors import ConfigError, LeaseLostError, WorkspaceError
from tempo.product_repairs import review

from .intake_views import error_message, page, signed_in
from .models import AgentRun
from .product_views import controller_for


@ensure_csrf_cookie
def repair(request, brief_id, run_id):
    if response := signed_in(request):
        return response
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    run = get_object_or_404(AgentRun, pk=run_id, execution_plan__brief_revision__brief_id=brief_id)
    product = run.execution_plan.brief_revision.brief
    info, error = None, ""
    instructions, paths = request.POST.get("instructions", ""), request.POST.get("paths", "")
    try:
        if request.method == "POST":
            key = str(uuid.UUID(request.POST.get("request_key", "")))
            success, message = async_to_sync(controller_for(product).control_run)(
                run.pk, "product_repair",
                {"expected_digest": request.POST.get("expected_digest", ""),
                 "instructions": instructions, "paths": paths},
                user_id=request.user.pk, idempotency_key=f"product-repair:{key}",
            )
            if not success:
                raise ValueError(message)
            return redirect(f"/ideas/{brief_id}/#execution")
        info = review(run)
    except (
        ValueError, ConfigError, WorkspaceError, LeaseLostError, OSError, IntegrityError,
    ) as exc:
        error = error_message(exc)
        if request.method == "POST":
            try:
                run.refresh_from_db()
                info = review(run)
            except (ValueError, ConfigError, WorkspaceError, OSError):
                pass
    response = page(
        request, "repair", product=product, run=run, repair=info, error=error,
        instructions=instructions, paths=paths, request_key=uuid.uuid4(),
    )
    if error:
        response.status_code = 409
    return response
