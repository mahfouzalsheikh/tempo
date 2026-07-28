from __future__ import annotations

import asyncio
import json
import mimetypes
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
from django.views.decorators.csrf import csrf_exempt

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


@csrf_exempt
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
