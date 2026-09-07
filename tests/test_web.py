import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import AsyncRequestFactory, Client

from tempo.runtime import set_orchestrator
from tempo_web.jwt_auth import decode_access_token
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

    async def control_run(self, run_id, action, payload, *, user_id, idempotency_key):
        self.control = {
            "run_id": run_id,
            "action": action,
            "payload": payload,
            "user_id": user_id,
            "idempotency_key": idempotency_key,
        }
        return True, "applied"

    def platform_snapshot(self):
        return {
            "projects": [
                {
                    "key": "acme/api",
                    "agents": {},
                    "runtime_providers": {},
                    "model_providers": {},
                    "tool_providers": {},
                    "workflow": {"nodes": [], "edges": []},
                }
            ]
        }

    async def update_platform_config(self, organization, project, sections):
        self.platform_update = (organization, project, sections)
        return True


def test_health_and_state():
    set_orchestrator(FakeOrchestrator())
    client = Client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/state").status_code == 200
    assert client.get("/api/v1/admin").status_code == 200
    assert client.get("/api/v1/A-1").json()["identifier"] == "A-1"
    assert client.get("/api/v1/missing").status_code == 404
    dashboard = client.get("/")
    assert b"Your work, at a glance." in dashboard.content
    assert b"Approval inbox" in dashboard.content
    assert b"Projects" in dashboard.content
    assert dashboard.content.count(b'<nav class="side-nav"') == 1
    assert b"Django admin" not in dashboard.content
    assert "csrftoken" in dashboard.cookies
    runtime = client.get("/ops/")
    assert b"Runtime" in runtime.content
    assert runtime.content.count(b'<nav class="side-nav"') == 1
    assert b'<nav class="topnav"' not in runtime.content
    assert b"Django Admin" not in runtime.content
    configuration = client.get("/ops/configuration/")
    assert b"Configuration" in configuration.content
    assert configuration.content.count(b'<nav class="side-nav"') == 1
    assert b'<nav class="topnav"' not in configuration.content
    assert b"Django Admin" not in configuration.content
    for response in (dashboard, runtime, configuration):
        assert b'class="sidebar"' in response.content
        assert b'class="topnav"' not in response.content
    assert client.get("/admin/").status_code == 302
    stylesheet = client.get("/static/tempo.css")
    assert stylesheet.status_code == 200
    assert b"--content-max: 1440px" in stylesheet.content
    assert b"font: 16px/1.6" in stylesheet.content
    assert b"grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet.content
    assert b"@media (max-width: 960px)" in stylesheet.content
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
    assert b"Workflow configurations" in response.content


@pytest.mark.django_db
def test_jwt_login_cookie_bearer_and_logout_flow():
    user = get_user_model().objects.create_user(username="operator", password="correct-horse")
    client = Client(enforce_csrf_checks=True)

    login_page = client.get("/login/?next=/ops/")
    assert login_page.status_code == 200
    assert b"Sign in to Tempo" in login_page.content
    assert b"/admin/login/" not in login_page.content
    csrf_token = client.cookies["csrftoken"].value

    invalid = client.post(
        "/api/v1/auth/login",
        data='{"username":"operator","password":"wrong"}',
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert invalid.status_code == 401
    assert settings.JWT_COOKIE_NAME not in invalid.cookies

    response = client.post(
        "/api/v1/auth/login",
        data='{"username":"operator","password":"correct-horse","next":"/ops/"}',
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["redirect"] == "/ops/"
    claims = decode_access_token(payload["access_token"])
    assert claims["sub"] == str(user.pk)
    assert claims["username"] == "operator"
    cookie = response.cookies[settings.JWT_COOKIE_NAME]
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Lax"

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "operator"
    assert me.json()["authentication"] == "jwt"
    dashboard = client.get("/")
    assert b"Operator session" in dashboard.content
    assert b"Sign out" in dashboard.content

    bearer_client = Client()
    bearer_me = bearer_client.get(
        "/api/v1/auth/me",
        HTTP_AUTHORIZATION=f"Bearer {payload['access_token']}",
    )
    assert bearer_me.status_code == 200
    assert bearer_me.json()["authentication"] == "jwt"
    assert (
        bearer_client.get("/api/v1/auth/me", HTTP_AUTHORIZATION="Bearer invalid").status_code == 401
    )

    orchestrator = FakeOrchestrator()
    set_orchestrator(orchestrator)
    bearer_mutation = Client(enforce_csrf_checks=True).post(
        "/api/v1/runs/7/reprioritize",
        data='{"priority":2}',
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {payload['access_token']}",
        HTTP_IDEMPOTENCY_KEY="jwt-action-7",
    )
    assert bearer_mutation.status_code == 202
    assert orchestrator.control["user_id"] == user.pk
    set_orchestrator(None)

    logout = client.post("/logout/", HTTP_X_CSRFTOKEN=csrf_token)
    assert logout.status_code == 302
    assert logout.url == "/login/"
    assert logout.cookies[settings.JWT_COOKIE_NAME]["max-age"] == 0
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.django_db
def test_jwt_login_rejects_missing_csrf_and_open_redirects():
    get_user_model().objects.create_user(username="operator", password="secret")
    csrf_client = Client(enforce_csrf_checks=True)
    assert (
        csrf_client.post(
            "/api/v1/auth/login",
            data='{"username":"operator","password":"secret"}',
            content_type="application/json",
        ).status_code
        == 403
    )

    client = Client()
    response = client.post(
        "/api/v1/auth/login",
        data=(
            '{"username":"operator","password":"secret","next":"https://attacker.example/steal"}'
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["redirect"] == "/"


@pytest.mark.django_db
def test_operator_actions_require_authentication_and_are_forwarded():
    orchestrator = FakeOrchestrator()
    set_orchestrator(orchestrator)
    client = Client()
    assert client.post("/api/v1/refresh").status_code == 401
    assert client.post("/api/v1/runs/7/pause", data={}).status_code == 401
    user = get_user_model().objects.create_user(username="operator", password="secret")
    client.force_login(user)
    response = client.post(
        "/api/v1/runs/7/reprioritize",
        data='{"priority": 2}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="action-7",
    )
    assert response.status_code == 202
    assert orchestrator.control == {
        "run_id": 7,
        "action": "reprioritize",
        "payload": {"priority": 2},
        "user_id": user.pk,
        "idempotency_key": "action-7",
    }
    set_orchestrator(None)


@pytest.mark.django_db
def test_platform_management_requires_authentication_and_audits_updates():
    from tempo_web.models import Organization, PlatformConfigurationChange, Project

    organization = Organization.objects.create(name="Acme", slug="acme")
    Project.objects.create(organization=organization, name="API", slug="api")
    orchestrator = FakeOrchestrator()
    set_orchestrator(orchestrator)
    client = Client()
    assert client.get("/api/v1/platform").status_code == 401
    assert client.post("/api/v1/platform/acme/api", data={}).status_code == 401
    user = get_user_model().objects.create_user(username="configurator", password="secret")
    client.force_login(user)
    assert client.get("/api/v1/platform").status_code == 200
    response = client.post(
        "/api/v1/platform/acme/api",
        data='{"workflow":{"nodes":[{"id":"delivery","agent":"implementer"}]}}',
        content_type="application/json",
    )
    assert response.status_code == 202
    assert orchestrator.platform_update[0:2] == ("acme", "api")
    change = PlatformConfigurationChange.objects.get()
    assert change.status == PlatformConfigurationChange.Status.APPLIED
    assert change.requested_by == user
    set_orchestrator(None)


@pytest.mark.django_db
def test_approval_decision_can_edit_arguments_and_requeues_an_unleased_run():
    from tempo.domain import utcnow
    from tempo_web.models import (
        AgentRun,
        ApprovalRequest,
        Organization,
        Project,
        TrackedIssue,
    )

    organization = Organization.objects.create(name="Acme", slug="acme")
    project = Project.objects.create(organization=organization, name="API", slug="api")
    issue = TrackedIssue.objects.create(
        project=project,
        tracker_kind="memory",
        external_id="1",
        identifier="A-1",
        title="Approve",
        state="open",
    )
    run = AgentRun.objects.create(
        project=project,
        issue=issue,
        status=AgentRun.Status.WAITING_APPROVAL,
        started_at=utcnow(),
    )
    approval = ApprovalRequest.objects.create(
        run=run,
        request_key="approval-1",
        kind="tool:github_api",
        title="Create pull request",
        details={},
        proposed_arguments={"method": "POST"},
    )
    user = get_user_model().objects.create_user(username="approver", password="secret")
    client = Client()
    assert client.get("/api/v1/approvals").status_code == 401
    client.force_login(user)
    response = client.post(
        f"/api/v1/approvals/{approval.pk}/decision",
        data='{"decision":"approve","arguments":{"method":"POST","body":{"draft":true}}}',
        content_type="application/json",
    )
    assert response.status_code == 200
    approval.refresh_from_db()
    run.refresh_from_db()
    assert approval.status == ApprovalRequest.Status.APPROVED
    assert approval.edited_arguments["body"]["draft"] is True
    assert approval.decided_by == user
    assert run.status == AgentRun.Status.RETRY_SCHEDULED


@pytest.mark.django_db
def test_control_state_surfaces_paused_and_safety_stopped_runs():
    from tempo.domain import utcnow
    from tempo_web.models import AgentRun, Organization, Project, TrackedIssue

    organization = Organization.objects.create(name="Acme", slug="acme")
    project = Project.objects.create(organization=organization, name="API", slug="api")
    issue = TrackedIssue.objects.create(
        project=project,
        tracker_kind="memory",
        external_id="1",
        identifier="A-1",
        title="Needs attention",
        state="open",
    )
    paused = AgentRun.objects.create(
        project=project,
        issue=issue,
        status=AgentRun.Status.PAUSED,
        phase="Paused",
        priority=2,
        started_at=utcnow(),
    )
    stopped = AgentRun.objects.create(
        project=project,
        issue=issue,
        status=AgentRun.Status.FAILED,
        phase="SafetyLimitReached",
        priority=1,
        started_at=utcnow(),
    )
    AgentRun.objects.create(
        project=project,
        issue=issue,
        status=AgentRun.Status.SUCCEEDED,
        phase="NoChangesRequired",
        started_at=utcnow(),
    )
    client = Client()
    assert client.get("/api/v1/control").status_code == 401
    user = get_user_model().objects.create_user(username="operator", password="secret")
    client.force_login(user)

    response = client.get("/api/v1/control")

    assert response.status_code == 200
    assert [row["run_id"] for row in response.json()["runs"]] == [stopped.pk, paused.pk]


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
