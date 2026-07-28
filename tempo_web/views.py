from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from asgiref.sync import async_to_sync
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.shortcuts import render

from tempo.runtime import get_orchestrator

STATIC_ROOT = Path(__file__).resolve().parent / "static"


async def static_asset(request: HttpRequest, name: str) -> HttpResponse:
    path = (STATIC_ROOT / name).resolve(strict=False)
    try:
        path.relative_to(STATIC_ROOT)
    except ValueError as exc:
        raise Http404 from exc
    if not path.is_file():  # noqa: ASYNC240 - two small bundled assets
        raise Http404
    content = await asyncio.to_thread(path.read_bytes)
    response = HttpResponse(
        content, content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    response["Cache-Control"] = "public, max-age=3600"
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
    orchestrator = get_orchestrator()
    if not orchestrator:
        return JsonResponse({"error": "orchestrator_unavailable"}, status=503)
    async_to_sync(orchestrator.refresh)()
    return JsonResponse({"status": "refresh_scheduled"}, status=202)


def dashboard(request: HttpRequest) -> HttpResponse:
    orchestrator = get_orchestrator()
    snapshot = orchestrator.snapshot() if orchestrator else None
    return render(request, "dashboard.html", {"snapshot": snapshot})


def admin_runtime(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_runtime.html")


def admin_configuration(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_configuration.html")
