import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.test import Client
from django.utils import timezone
from test_acceptance import report_for
from test_preview_health import passed as health_passed
from test_preview_health import published  # noqa: F401
from test_rollback_rehearsal import (  # noqa: F401
    active_baseline,
    bundle,
    configured,
    product_factory,
    saved_artifact,
    staging,
)
from test_rollback_rehearsal import passed as rehearsed

from tempo import acceptance, preview_health, release_files, releases, rollback_rehearsal
from tempo.preview_probe import expected_report
from tempo_web.models import BriefRevision, ReleaseDeployment

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ready(staging, published, monkeypatch):  # noqa: F811
    artifact, _, _ = staging
    monkeypatch.setenv("TEMPO_ACCEPTANCE_IMAGE", "sha256:" + "b" * 64)
    instructions = {
        c["id"]: 'open /\nexpect text "Saved build"'
        for c in artifact.run.product_snapshot["brief"]["criteria"]
    }
    suite = acceptance.approve(artifact.pk, instructions, 0, artifact.user.pk)
    acceptance.enqueue(artifact.pk, suite.pk, suite.digest, uuid.uuid4(), artifact.user.pk)
    job = acceptance.claim()
    acceptance.finish(job, report_for(suite, artifact))
    health_passed(published)
    rehearsed(staging)
    assert releases.delivery(artifact)["can_publish"]
    return staging


def enqueue(ready, key=None):
    artifact, configuration, _ = ready
    info = releases.delivery(artifact)
    return releases.enqueue(
        artifact.pk,
        configuration.pk,
        configuration.digest,
        info.get("expected_target", ""),
        key or uuid.uuid4(),
        artifact.user.pk,
    )


def published_release(ready):
    enqueue(ready)
    job = releases.claim()
    releases.process(job)
    job.refresh_from_db()
    assert job.status == "published", job.error
    return job


def recover(job):
    ReleaseDeployment.objects.filter(pk=job.pk).update(
        deadline=timezone.now() - timedelta(seconds=1)
    )
    releases.recover()
    worker = releases.claim()
    assert worker and worker.status == "recovering"
    releases.process(worker)
    job.refresh_from_db()
    return job


def test_publish_exact_verified_bundle_and_explicitly_withdraw_first_release(ready):
    artifact, configuration, root = ready
    job = published_release(ready)
    assert release_files.pointer(root, configuration.target.slot.hex) == releases.candidate_pointer(
        job
    )
    assert releases.delivery(artifact)["active"].pk == job.pk
    assert not releases.delivery(artifact)["can_publish"]
    assert job.report["checks"] == expected_report(artifact.manifest["files"])
    assert job.authorization["assessment"]["ready"]
    assert release_files.pointer(root, job.probe_slot.hex) is None
    releases.request_rollback(job, artifact.user.pk)
    worker = releases.claim()
    releases.process(worker)
    job.refresh_from_db()
    assert job.status == "rolled_back", job.error
    assert job.report["checks"] and job.recovery_report["checks"]["status"] == 404
    assert release_files.pointer(root, configuration.target.slot.hex) is None
    assert releases.delivery(artifact)["can_publish"]


def test_rollback_restores_a_different_retained_previous_bundle(ready):
    prior = active_baseline(ready, different=True)
    rehearsed(ready)
    job = published_release(ready)
    releases.request_rollback(job, ready[0].user.pk)
    worker = releases.claim()
    releases.process(worker)
    job.refresh_from_db()
    assert job.status == "rolled_back", job.error
    assert releases.delivery(ready[0])["restored"].pk == job.pk
    current = release_files.pointer(ready[2], ready[1].target.slot.hex)
    assert current["release_id"] == prior["release_id"]
    assert job.recovery_report["checks"]["root_digest"] != job.report["checks"]["root_digest"]


@pytest.mark.parametrize("change", ["scope", "health", "rehearsal"])
def test_changed_gate_after_enqueue_cannot_activate_target(ready, change):
    artifact, configuration, root = ready
    enqueue(ready)
    job = releases.claim()
    if change == "scope":
        old = artifact.run.execution_plan.brief_revision
        BriefRevision.objects.create(
            brief=old.brief,
            number=old.number + 1,
            specification=old.specification,
            digest=old.digest,
            created_by=artifact.user,
        )
    elif change == "health":
        deployment = artifact.preview
        preview_health.enqueue(
            artifact.pk, preview_health.generation(deployment), uuid.uuid4(), artifact.user.pk
        )
    else:
        artifact.release_configurations.first().rehearsals.update(report={})
    releases.process(job)
    job.refresh_from_db()
    assert job.status == "rollback_queued"
    assert release_files.pointer(root, configuration.target.slot.hex) is None
    recover(job)
    assert job.status == "rolled_back", job.error


@pytest.mark.parametrize("boundary", ["queued", "prepared", "activated", "cleaned"])
def test_worker_crash_at_each_boundary_restores_previous_state_and_fences_stale_owner(
    ready, boundary
):
    enqueue(ready)
    job = releases.claim()
    if boundary != "queued":
        releases.prepare(job)
    if boundary in {"activated", "cleaned"}:
        releases.activate(job, expected_report(ready[0].manifest["files"]))
    if boundary == "cleaned":
        releases.cleanup_preflight(job)
    stale = ReleaseDeployment.objects.get(pk=job.pk)
    recover(job)
    assert job.status == "rolled_back", job.error
    assert release_files.pointer(ready[2], ready[1].target.slot.hex) is None
    assert release_files.pointer(ready[2], job.probe_slot.hex) is None
    with pytest.raises(releases.LeaseLost):
        releases.prepare(stale)


def test_lost_commit_after_filesystem_activation_is_reconciled_by_rollback(ready, monkeypatch):
    enqueue(ready)
    job = releases.claim()
    releases.prepare(job)
    original = release_files.activate

    class Crash(BaseException):
        pass

    def activate(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(release_files, "activate", activate)
    with pytest.raises(Crash):
        releases.activate(job, expected_report(ready[0].manifest["files"]))
    job.refresh_from_db()
    assert job.authorization == {}
    assert release_files.pointer(ready[2], ready[1].target.slot.hex) == releases.candidate_pointer(
        job
    )
    monkeypatch.setattr(release_files, "activate", original)
    recover(job)
    assert job.status == "rolled_back", job.error


def test_failed_post_publish_health_automatically_restores_target(ready, monkeypatch):
    original = releases.probe

    def probe(host, port, public_port, slot, files):
        if slot == ready[1].target.slot.hex:
            raise ValueError("unhealthy")
        return original(host, port, public_port, slot, files)

    monkeypatch.setattr(releases, "probe", probe)
    enqueue(ready)
    job = releases.claim()
    releases.process(job)
    job.refresh_from_db()
    assert job.status == "rollback_queued"
    recover(job)
    assert job.status == "rolled_back"


def test_unrecognized_target_owner_is_never_overwritten_and_requires_intervention(ready):
    enqueue(ready)
    job = releases.claim()
    releases.prepare(job)
    releases.activate(job, expected_report(ready[0].manifest["files"]))
    replacement = release_files.activate(
        ready[2],
        ready[1].target.slot.hex,
        job.bundle.hex,
        ready[1].digest,
        releases.operation(job, "publish"),
        uuid.uuid4().hex,
    )
    for _ in range(3):
        recover(job)
    assert job.status == "needs_attention"
    assert release_files.pointer(ready[2], ready[1].target.slot.hex) == replacement
    assert not releases.delivery(ready[0])["can_publish"]
    with pytest.raises(ValueError, match="pending"):
        rollback_rehearsal.enqueue(
            ready[0].pk, ready[1].pk, ready[1].digest, uuid.uuid4(), ready[0].user.pk
        )


def test_repeated_publication_request_after_lost_response_never_publishes_twice(ready):
    key = uuid.uuid4()
    original = enqueue(ready, key)
    job = releases.claim()
    releases.process(job)
    again = releases.enqueue(
        ready[0].pk, ready[1].pk, ready[1].digest, "stale", key, ready[0].user.pk
    )
    assert again.pk == original.pk
    assert releases.claim() is None


def test_concurrent_publications_reserve_target_once(ready):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    barrier = threading.Barrier(2)

    def submit():
        try:
            barrier.wait(timeout=5)
            try:
                enqueue(ready)
                return True
            except ValueError:
                return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert sorted(results) == [False, True]
    assert ReleaseDeployment.objects.count() == 1


def test_publication_authentication_csrf_scope_review_and_private_export(ready):
    artifact, configuration, _ = ready
    url = artifact.url.replace("preview/", "publish/")
    client = Client()
    assert client.post(url).status_code == 302
    client.force_login(artifact.user)
    assert client.get(url).status_code == 405
    data = {
        "configuration_id": configuration.pk,
        "configuration_digest": configuration.digest,
        "expected_target": releases.delivery(artifact)["expected_target"],
        "request_key": uuid.uuid4(),
    }
    assert client.post(url, data).status_code == 302
    assert not ReleaseDeployment.objects.exists()
    data["reviewed"] = "on"
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(artifact.user)
    assert csrf.post(url, data).status_code == 403
    assert (
        client.post(url.replace(f"runs/{artifact.run_id}/", "runs/999999/"), data).status_code
        == 404
    )
    assert client.post(url, data).status_code == 302
    job = ReleaseDeployment.objects.get()
    receipt = artifact.url.replace("preview/", f"releases/{job.pk}/evidence/")
    response = client.get(receipt)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert response.json()["release"]["identity_digest"] == job.identity_digest


def test_lease_expiry_during_archive_verification_cannot_switch_named_target(ready, monkeypatch):
    enqueue(ready)
    job = releases.claim()
    releases.prepare(job)
    original = release_files.activate
    clock = timezone.now

    def delay(*args, **kwargs):
        authorize = kwargs["before_commit"]

        def late():
            monkeypatch.setattr(releases.timezone, "now", lambda: clock() + timedelta(minutes=4))
            authorize()

        kwargs["before_commit"] = late
        return original(*args, **kwargs)

    monkeypatch.setattr(release_files, "activate", delay)
    with pytest.raises(releases.LeaseLost):
        releases.activate(job, expected_report(ready[0].manifest["files"]))
    assert release_files.pointer(ready[2], ready[1].target.slot.hex) is None


def test_preflight_cannot_promote_after_health_receipt_expires(ready, monkeypatch):
    enqueue(ready)
    job = releases.claim()
    releases.prepare(job)
    original = release_files.activate
    clock = timezone.now
    # Extend only the worker lease to distinguish evidence expiry from lease expiry.
    ReleaseDeployment.objects.filter(pk=job.pk).update(deadline=clock() + timedelta(minutes=10))

    def delay(*args, **kwargs):
        authorize = kwargs["before_commit"]

        def late():
            monkeypatch.setattr(releases.timezone, "now", lambda: clock() + timedelta(minutes=6))
            authorize()

        kwargs["before_commit"] = late
        return original(*args, **kwargs)

    monkeypatch.setattr(release_files, "activate", delay)
    with pytest.raises(ValueError, match="expired"):
        releases.activate(job, expected_report(ready[0].manifest["files"]))
    assert release_files.pointer(ready[2], ready[1].target.slot.hex) is None


def test_recovery_can_be_retried_after_operator_repairs_unknown_target(ready):
    enqueue(ready)
    job = releases.claim()
    releases.prepare(job)
    releases.activate(job, expected_report(ready[0].manifest["files"]))
    replacement = release_files.activate(
        ready[2],
        ready[1].target.slot.hex,
        job.bundle.hex,
        ready[1].digest,
        releases.operation(job, "publish"),
        uuid.uuid4().hex,
    )
    for _ in range(3):
        recover(job)
    assert job.status == "needs_attention"
    release_files.remove_target(ready[2], ready[1].target.slot.hex, replacement["operation_id"])
    releases.request_rollback(job, ready[0].user.pk, retry=True)
    worker = releases.claim()
    releases.process(worker)
    job.refresh_from_db()
    assert job.status == "rolled_back"


def test_release_export_omits_private_preflight_address_and_tamper_removes_active_link(ready):
    job = published_release(ready)
    assert job.probe_slot.hex not in str(job.identity)
    ReleaseDeployment.objects.filter(pk=job.pk).update(report={})
    assert not releases.delivery(ready[0]).get("active")
