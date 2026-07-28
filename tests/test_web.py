import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncRequestFactory, Client

from tempo.runtime import set_orchestrator
from tempo_web.views import state_events


class FakeOrchestrator:
    def __init__(self):
        self.event_queue = None

    def snapshot(self):
        return {
            "service": {"max_concurrent_agents": 1},
            "running": [],
            "retries": [],
            "claimed_count": 0,
            "completed_count": 0,
            "totals": {"total_tokens": 0, "validation_passes": 0},
        }

    def issue_snapshot(self, identifier):
        return {"identifier": identifier} if identifier == "A-1" else None

    def admin_snapshot(self):
        return {
            "service": {},
            "workflow": {},
            "tracker": {},
            "agents": {},
            "validation": {},
            "hooks": {},
            "runtime": {},
        }

    def subscribe_events(self):
        import asyncio

        self.event_queue = asyncio.Queue(maxsize=1)
        return self.event_queue

    def unsubscribe_events(self, queue):
        if self.event_queue is queue:
            self.event_queue = None


def test_health_and_state():
    set_orchestrator(FakeOrchestrator())
    client = Client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/state").status_code == 200
    assert client.get("/api/v1/admin").status_code == 200
    assert client.get("/api/v1/A-1").json()["identifier"] == "A-1"
    assert client.get("/api/v1/missing").status_code == 404
    assert b"Tempo" in client.get("/").content
    assert b"Runtime" in client.get("/ops/").content
    assert b"Configuration" in client.get("/ops/configuration/").content
    assert client.get("/admin/").status_code == 302
    assert client.get("/static/tempo.css").status_code == 200
    assert client.get("/static/admin/css/base.css").status_code == 200
    set_orchestrator(None)


@pytest.mark.django_db
def test_django_admin_lists_tempo_models():
    user = get_user_model().objects.create_superuser(
        username="operator",
        email="operator@example.com",
        password="secret",
    )
    client = Client()
    client.force_login(user)
    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"Tempo administration" in response.content
    assert b"Tracked issues" in response.content
    assert b"Agent runs" in response.content
    assert b"Validation attempts" in response.content


@pytest.mark.asyncio
async def test_live_event_stream_sends_initial_state_and_unsubscribes():
    orchestrator = FakeOrchestrator()
    set_orchestrator(orchestrator)
    request = AsyncRequestFactory().get("/api/v1/events")
    response = await state_events(request)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    iterator = response.streaming_content.__aiter__()
    first = await anext(iterator)
    assert first.startswith(b"data:")
    await iterator.aclose()
    orchestrator.unsubscribe_events(orchestrator.event_queue)
    set_orchestrator(None)
