from __future__ import annotations

import json
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from pydantic import ValidationError

from tempo.contracts.intake import Brief
from tempo.errors import ConfigError
from tempo.intake import (
    IntakeConflict,
    approve_plan,
    checked_brief,
    checked_plan,
    create_brief,
    revise_brief,
    revise_plan,
)

from .intake_forms import BriefForm, CriterionFormSet, TaskFormSet
from .models import ProductBrief, Project


def error_message(error):
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors()
        )
    if isinstance(error, IntegrityError):
        return "Another submission changed this record. Reload before trying again."
    return str(error)


def detail(product, revision_number=None, plan_number=None):
    revision = (
        get_object_or_404(product.revisions, number=revision_number)
        if revision_number is not None
        else product.revisions.first()
    )
    plan = (
        get_object_or_404(revision.plans, number=plan_number)
        if plan_number is not None
        else revision.plans.first()
    )
    brief = checked_brief(revision)
    contract, waves = checked_plan(plan, brief)
    latest_revision = product.revisions.first()
    current = revision.pk == latest_revision.pk and plan.pk == latest_revision.plans.first().pk
    task_map = {task.id: task for task in contract.tasks}
    return {
        "product": product,
        "revision": revision,
        "plan": plan,
        "brief": brief,
        "contract": contract,
        "waves": [[task_map[key] for key in wave] for wave in waves],
        "is_current": current,
    }


def page(request, mode, **context):
    return render(request, "intake.html", {"mode": mode, **context})


def signed_in(request):
    if not request.user.is_authenticated:
        return redirect(f"/login/?next={request.path}")
    return None


@ensure_csrf_cookie
def ideas(request):
    if response := signed_in(request):
        return response
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    products = ProductBrief.objects.select_related("project", "project__organization").order_by(
        "-created_at",
    )[:100]
    cards = []
    for product in products:
        revision = product.revisions.first()
        plan = revision.plans.first()
        cards.append(
            {
                "product": product,
                "revision": revision,
                "plan": plan,
                "title": revision.specification["title"],
            }
        )
    return page(request, "list", cards=cards)


@ensure_csrf_cookie
def edit_brief(request, brief_id=None):
    if response := signed_in(request):
        return response
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    product = get_object_or_404(ProductBrief, pk=brief_id) if brief_id else None
    revision = product.revisions.first() if product else None
    initial = dict(revision.specification) if revision else {}
    initial.update(
        project=product.project_id
        if product
        else Project.objects.filter(
            active=True,
        )
        .values_list("pk", flat=True)
        .first(),
        request_key=uuid.uuid4(),
        expected_revision=revision.number if revision else None,
    )
    form = BriefForm(request.POST if request.method == "POST" else None, initial=initial)
    if product:
        form.fields["project"].disabled = True
    criteria = (
        CriterionFormSet(
            request.POST if request.method == "POST" else None,
            prefix="criteria",
            initial=[
                {"criterion_id": item["id"], **item} for item in initial.get("criteria", [{}])
            ],
        )
        if revision
        else CriterionFormSet(
            request.POST if request.method == "POST" else None,
            prefix="criteria",
            initial=[{"criterion_id": f"AC-{uuid.uuid4().hex[:12]}"}],
        )
    )
    error = ""
    if request.method == "POST":
        valid = form.is_valid()
        valid = criteria.is_valid() and valid
        if valid:
            specification = {
                key: form.cleaned_data[key]
                for key in Brief.model_fields
                if key not in {"schema_version", "criteria"}
            }
            specification["criteria"] = [
                {
                    "id": row["criterion_id"] or f"AC-{uuid.uuid4().hex[:12]}",
                    "outcome": row["outcome"],
                    "verification": row["verification"],
                }
                for row in criteria.cleaned_data
                if row and not row.get("DELETE")
            ]
            try:
                if product:
                    revise_brief(
                        product.pk,
                        expected_revision=form.cleaned_data["expected_revision"],
                        specification=specification,
                        user_id=request.user.pk,
                    )
                else:
                    product = create_brief(
                        project_id=form.cleaned_data["project"].pk,
                        specification=specification,
                        user_id=request.user.pk,
                        request_key=form.cleaned_data["request_key"],
                    )
                return redirect("idea_detail", brief_id=product.pk)
            except (ValueError, IntegrityError) as exc:
                error = error_message(exc)
    response = page(request, "edit", form=form, criteria=criteria, product=product, error=error)
    if request.method == "POST":
        response.status_code = 400
    return response


@ensure_csrf_cookie
def idea_detail(request, brief_id):
    if response := signed_in(request):
        return response
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    product = get_object_or_404(ProductBrief, pk=brief_id)
    try:
        context = detail(product, request.GET.get("revision"), request.GET.get("plan"))
    except ValueError as exc:
        return page(request, "error", error=error_message(exc))
    from .product_views import run_details
    return page(request, "detail", runs=run_details(context["plan"]), **context)


@ensure_csrf_cookie
def edit_plan(request, brief_id):
    if response := signed_in(request):
        return response
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    product = get_object_or_404(ProductBrief, pk=brief_id)
    context = detail(product)
    initial = [
        {
            **task.model_dump(),
            "criteria": ", ".join(task.criteria),
            "depends_on": ", ".join(task.depends_on),
        }
        for task in context["contract"].tasks
    ]
    tasks = TaskFormSet(
        request.POST if request.method == "POST" else None, prefix="tasks", initial=initial
    )
    error = ""
    if request.method == "POST" and tasks.is_valid():
        rows = []
        for row in tasks.cleaned_data:
            if row and not row.get("DELETE"):
                rows.append(
                    {
                        key: (
                            [value.strip() for value in row[key].split(",") if value.strip()]
                            if key in {"criteria", "depends_on"}
                            else row[key]
                        )
                        for key in ["id", "title", "role", "instructions", "criteria", "depends_on"]
                    }
                )
        try:
            revise_plan(
                product.pk,
                expected_plan_id=int(request.POST.get("expected_plan_id", "0")),
                specification={"brief_digest": context["revision"].digest, "tasks": rows},
                user_id=request.user.pk,
            )
            return redirect("idea_detail", brief_id=product.pk)
        except (ValueError, IntegrityError) as exc:
            error = error_message(exc)
    response = page(
        request,
        "plan",
        tasks=tasks,
        error=error,
        expected_plan_id=request.POST.get("expected_plan_id", context["plan"].pk),
        **context,
    )
    if request.method == "POST":
        response.status_code = 400
    return response


def approve(request, brief_id):
    if response := signed_in(request):
        return response
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        approve_plan(
            brief_id,
            expected_plan_id=int(request.POST.get("expected_plan_id", "0")),
            expected_digest=request.POST.get("expected_digest", ""),
            user_id=request.user.pk,
        )
    except ObjectDoesNotExist:
        return JsonResponse({"error": "Brief not found or project inactive."}, status=404)
    except (ValueError, IntegrityError) as exc:
        return page(request, "error", error=error_message(exc), brief_id=brief_id)
    return redirect("idea_detail", brief_id=brief_id)


def serialize(product, revision_number=None, plan_number=None):
    context = detail(product, revision_number, plan_number)
    revision, plan = context["revision"], context["plan"]
    from .product_views import run_details
    runs = run_details(plan)
    return {
        "id": product.pk,
        "project_id": product.project_id,
        "revision": revision.number,
        "brief": revision.specification,
        "brief_digest": revision.digest,
        "plan_id": plan.pk,
        "plan_revision": plan.number,
        "plan": plan.specification,
        "plan_digest": plan.digest,
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
        "execution_started": bool(runs),
        "runs": runs,
        "is_current": context["is_current"],
        "history": [
            {
                "revision": item.number,
                "digest": item.digest,
                "plans": [
                    {
                        "id": plan.pk,
                        "number": plan.number,
                        "digest": plan.digest,
                        "approved": bool(plan.approved_at),
                    }
                    for plan in item.plans.all()
                ],
            }
            for item in product.revisions.all()
        ],
    }


def api(request, brief_id=None, action=None):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    if request.method not in ({"POST"} if action else {"GET", "POST"}):
        return HttpResponseNotAllowed(["POST"] if action else ["GET", "POST"])
    try:
        if request.method == "GET":
            if brief_id:
                return JsonResponse(
                    serialize(
                        get_object_or_404(ProductBrief, pk=brief_id),
                        request.GET.get("revision"),
                        request.GET.get("plan"),
                    )
                )
            return JsonResponse(
                {
                    "briefs": [
                        serialize(product)
                        for product in ProductBrief.objects.order_by("-created_at")[:100]
                    ],
                    "projects": list(Project.objects.filter(active=True).values("id", "name")),
                }
            )
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError("Request must be a JSON object.")
        if brief_id is None:
            product = create_brief(
                project_id=body["project_id"],
                specification=body["brief"],
                user_id=request.user.pk,
                request_key=body["request_key"],
            )
        else:
            product = get_object_or_404(ProductBrief, pk=brief_id)
            if action == "execute":
                from .product_views import launch
                launch(product, body, request.user.pk)
            elif action == "plans":
                revise_plan(
                    brief_id,
                    expected_plan_id=body["expected_plan_id"],
                    specification=body["plan"],
                    user_id=request.user.pk,
                )
            elif action == "approve":
                approve_plan(
                    brief_id,
                    expected_plan_id=body["expected_plan_id"],
                    expected_digest=body["expected_digest"],
                    user_id=request.user.pk,
                )
            elif action is None:
                revise_brief(
                    brief_id,
                    expected_revision=body["expected_revision"],
                    specification=body["brief"],
                    user_id=request.user.pk,
                )
            else:
                return JsonResponse({"error": "unsupported_action"}, status=404)
        return JsonResponse(serialize(product), status=201 if action != "approve" else 200)
    except ObjectDoesNotExist:
        return JsonResponse({"error": "Brief or active project not found."}, status=404)
    except (IntakeConflict, IntegrityError, ConfigError) as exc:
        return JsonResponse({"error": error_message(exc)}, status=409)
    except (ValueError, KeyError, TypeError) as exc:
        return JsonResponse({"error": error_message(exc)}, status=400)
