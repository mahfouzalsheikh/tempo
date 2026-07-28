from django.test import Client

from tempo.runtime import set_orchestrator


class FakeOrchestrator:
    def snapshot(self):
        return {
            "service": {"max_concurrent_agents": 1},
            "running": [],
            "retries": [],
            "completed_count": 0,
            "totals": {"total_tokens": 0},
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


def test_health_and_state():
    set_orchestrator(FakeOrchestrator())
    client = Client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/state").status_code == 200
    assert client.get("/api/v1/admin").status_code == 200
    assert client.get("/api/v1/A-1").json()["identifier"] == "A-1"
    assert client.get("/api/v1/missing").status_code == 404
    assert b"Tempo" in client.get("/").content
    assert b"Runtime" in client.get("/admin/").content
    assert b"Configuration" in client.get("/admin/configuration/").content
    assert client.get("/static/tempo.css").status_code == 200
    set_orchestrator(None)
