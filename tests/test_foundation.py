from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.control_plane import ControlPlane
from tempo.domain import Issue, utcnow
from tempo.persistence import PersistenceStore
from tempo_web.models import AgentRun, Project, TrackedIssue


def store_config(project_slug: str, root: Path):
    return SimpleNamespace(
        project=SimpleNamespace(
            organization="acme",
            slug=project_slug,
            name=project_slug.title(),
            environment="test",
            max_concurrent_runs=2,
            environment_max_concurrent_runs=1,
        ),
        workspace=SimpleNamespace(root=root),
        tracker=SimpleNamespace(provider={}),
        model_dump=lambda **_: {"project": project_slug},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_durable_claim_is_recovered_after_lease_expiry(tmp_path):
    store = PersistenceStore(
        "memory",
        config=store_config("kernel", tmp_path),
        workflow_path=tmp_path / "WORKFLOW.md",
    )
    await store.initialize()
    issue = Issue(id="1", identifier="K-1", title="Durable", state="open")
    run_id = await store.enqueue_issue(issue)
    assert run_id is not None
    assert await store.claim_run(run_id, "worker-a", lease_seconds=30)
    assert not await store.claim_run(run_id, "worker-b", lease_seconds=30)

    await AgentRun.objects.filter(pk=run_id).aupdate(
        lease_expires_at=utcnow() - timedelta(seconds=1)
    )
    await store.reconcile_incomplete_records()

    run = await AgentRun.objects.aget(pk=run_id)
    assert run.status == AgentRun.Status.RETRY_SCHEDULED
    assert run.phase == "Recovering"
    assert (await store.pending_runs())[0]["run_id"] == run_id
    assert await store.claim_run(run_id, "worker-b", lease_seconds=30)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_tracker_ids_are_isolated_by_project(tmp_path):
    first = PersistenceStore(
        "memory",
        config=store_config("alpha", tmp_path / "alpha"),
        workflow_path=tmp_path / "alpha.md",
    )
    second = PersistenceStore(
        "memory",
        config=store_config("beta", tmp_path / "beta"),
        workflow_path=tmp_path / "beta.md",
    )
    await first.initialize()
    await second.initialize()
    issue = Issue(id="shared", identifier="X-1", title="Shared id", state="open")
    assert await first.enqueue_issue(issue) is not None
    assert await second.enqueue_issue(issue) is not None

    assert await Project.objects.filter(organization__slug="acme").acount() == 2
    assert await TrackedIssue.objects.filter(external_id="shared").acount() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_control_plane_hosts_multiple_project_workflows(tmp_path):
    paths = []
    for slug in ("alpha", "beta"):
        path = tmp_path / f"{slug}.md"
        path.write_text(
            f"""---
project:
  organization: acme
  slug: {slug}
  name: {slug.title()}
tracker:
  kind: memory
  provider:
    issues: []
  active_states: [open]
  terminal_states: [closed]
workspace:
  root: {tmp_path / slug}
---
Work on {{{{ issue.identifier }}}}.
"""
        )
        paths.append(str(path))
    control_plane = ControlPlane(paths)
    await control_plane.start()
    try:
        snapshot = control_plane.snapshot()
        assert snapshot["service"]["project_count"] == 2
        assert {row["key"] for row in snapshot["projects"]} == {
            "acme/alpha",
            "acme/beta",
        }
    finally:
        await control_plane.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_operator_can_cancel_a_paused_durable_run(tmp_path):
    from django.contrib.auth import get_user_model

    from tempo.orchestrator import Orchestrator
    from tempo_web.models import OperatorAction

    store = PersistenceStore(
        "memory",
        config=store_config("controls", tmp_path),
        workflow_path=tmp_path / "WORKFLOW.md",
    )
    await store.initialize()
    issue = Issue(id="paused", identifier="C-1", title="Paused", state="open")
    run_id = await store.enqueue_issue(issue)
    assert run_id is not None
    await store.set_control_state(
        run_id,
        status=AgentRun.Status.PAUSED,
        phase="Paused",
    )
    user = await get_user_model().objects.acreate_user(username="operator")
    orchestrator = Orchestrator(str(tmp_path / "WORKFLOW.md"))
    orchestrator.persistence = store

    applied, message = await orchestrator.control_run(
        run_id,
        "cancel",
        {},
        user_id=user.pk,
        idempotency_key="cancel-paused",
    )

    assert applied is True
    assert message == "run cancelled"
    assert (await AgentRun.objects.aget(pk=run_id)).status == AgentRun.Status.CANCELLED
    assert (
        await OperatorAction.objects.filter(
            run_id=run_id,
            action="cancel",
            status=OperatorAction.Status.APPLIED,
        ).acount()
        == 1
    )
