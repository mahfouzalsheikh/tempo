import asyncio
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncRequestFactory, Client
from django.utils import timezone

from tempo.runtime import set_orchestrator
from tempo_web.jwt_auth import issue_access_token
from tempo_web.views import state_events


@pytest.mark.parametrize("path", [
    "/api/v1/state", "/api/v1/admin", "/api/v1/events", "/api/v1/GH-1",
    "/api/v1/control", "/api/v1/approvals", "/api/v1/platform",
])
def test_anonymous_reads_are_denied_before_accessing_runtime(path):
    class Unreadable:
        def __getattr__(self, name):
            raise AssertionError("unauthenticated request accessed runtime")

    set_orchestrator(Unreadable())
    try:
        response = Client().get(path)
        assert response.status_code == 401
        assert "no-store" in response["Cache-Control"]
        assert response.json() == {"error": "authentication_required"}
    finally:
        set_orchestrator(None)


@pytest.mark.parametrize("path", ["/", "/ops/", "/ops/configuration/"])
def test_private_pages_redirect_to_login_with_return_path(path):
    response = Client().get(path)
    assert response.status_code == 302
    assert response["Location"].startswith("/login/?next=")
    assert "no-store" in response["Cache-Control"]


@pytest.mark.django_db
def test_bearer_reads_are_private_and_inactive_users_are_denied():
    user = get_user_model().objects.create_user(username="reader", password="test-only")
    token, _ = issue_access_token(user)
    client = Client()
    runtime = SimpleNamespace(snapshot=lambda: {"private": "state"})
    set_orchestrator(runtime)
    try:
        response = client.get("/api/v1/state", HTTP_AUTHORIZATION=f"Bearer {token}")
        assert response.status_code == 200
        assert response.json() == {"private": "state"}
        assert "no-store" in response["Cache-Control"]
        assert "Authorization" in response["Vary"]
        user.is_active = False
        user.save()
        assert client.get("/api/v1/state", HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 401
    finally:
        set_orchestrator(None)


@pytest.mark.asyncio
async def test_event_stream_closes_at_token_expiry_and_releases_subscription():
    queue = asyncio.Queue()
    subscriptions = []
    runtime = SimpleNamespace(
        snapshot=lambda: {"private": "state"},
        subscribe_events=lambda: queue,
        unsubscribe_events=lambda value: subscriptions.append(value),
    )
    set_orchestrator(runtime)
    request = AsyncRequestFactory().get("/api/v1/events")
    request.tempo_access_expires_at = timezone.now().timestamp() + 0.02
    try:
        response = await state_events(request)
        async with asyncio.timeout(1):
            events = [event async for event in response.streaming_content]
        assert events[0].startswith(b"data:")
        assert subscriptions == [queue]
    finally:
        set_orchestrator(None)
