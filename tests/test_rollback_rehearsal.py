import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import HTTPServer

import pytest
from django.db import connection, connections
from django.test import Client
from django.utils import timezone
from test_release_configuration import (  # noqa: F401
    bundle,
    configured,
    product_factory,
    saved_artifact,
)

from tempo import release_configuration, release_files
from tempo import rollback_rehearsal as rehearsal
from tempo.acceptance_contract import digest
from tempo.build_artifacts import artifact_manifest, package_directory
from tempo.release_probe import absent_report
from tempo.release_server import ReleaseHandler
from tempo_web.models import BuildArtifact, DeploymentTarget, RollbackRehearsal, RunCheckpoint
from tempo_web.release_views import evaluate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def staging(configured, monkeypatch):  # noqa: F811
    configuration = release_configuration.approve(
        configured.pk, "mini-staging", 0, configured.user.pk
    )
    root, public_port = release_configuration.settings()
    server = HTTPServer(("127.0.0.1", 0), ReleaseHandler)
    server.root, server.public_port = root, public_port
    monkeypatch.setenv("TEMPO_RELEASE_HEALTH_HOST", "127.0.0.1")
    monkeypatch.setenv("TEMPO_RELEASE_HEALTH_PORT", str(server.server_port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield configured, configuration, root
    server.shutdown()
    server.server_close()
    thread.join()


def enqueue(staging, key=None):
    artifact, configuration, _ = staging
    return rehearsal.enqueue(
        artifact.pk, configuration.pk, configuration.digest, key or uuid.uuid4(), artifact.user.pk
    )


def passed(staging):
    enqueue(staging)
    job = rehearsal.claim()
    rehearsal.process(job)
    job.refresh_from_db()
    assert job.status == "passed", job.error
    return job


def active_baseline(staging, different=False):
    artifact, configuration, root = staging
    if different:
        output = root / "previous-source" / "dist"
        output.mkdir(parents=True)
        (output / "index.html").write_text("<h1>Previous release</h1>")
        packaged = package_directory(output.parent, "dist")
        checkpoint = artifact.run.checkpoints.filter(kind="product_candidate").first()
        candidate = dict(checkpoint.payload)
        manifest = artifact_manifest(artifact.run.product_snapshot, candidate, packaged)
        artifact = BuildArtifact.objects.create(
            run=artifact.run,
            digest=packaged["digest"],
            size=packaged["size"],
            data=packaged["data"],
            manifest=manifest,
            manifest_digest=digest(manifest),
        )
        candidate["artifact"] = {
            "id": artifact.pk,
            "sha256": artifact.digest,
            "size": artifact.size,
            "manifest_digest": artifact.manifest_digest,
        }
        RunCheckpoint.objects.create(
            run=artifact.run,
            kind="product_candidate",
            payload=candidate,
            sequence=2,
            idempotency_key="previous-release",
        )
        configuration = release_configuration.approve(
            artifact.pk, configuration.target.key, 0, configuration.approved_by_id
        )
    key = uuid.uuid4().hex
    release_files.prepare(
        root,
        key,
        bytes(artifact.data),
        artifact.manifest["files"],
        artifact.digest,
        configuration.digest,
    )
    pointer = release_files.activate(
        root, configuration.target.slot.hex, key, configuration.digest, None, uuid.uuid4().hex
    )
    return pointer


def test_first_release_rehearses_withdrawal_and_cleanup_without_changing_target(staging):
    artifact, configuration, root = staging
    job = passed(staging)
    assert job.report["checks"]["restoration"] == absent_report()
    assert job.report_digest == digest(job.report)
    assert rehearsal.summary(artifact)["passed"]
    report = evaluate(artifact)
    assert report["schema"] == 4
    assert {g["id"]: g["status"] for g in report["gates"]}["rollback"] == "passed"
    assert not report["ready"]  # Acceptance and preview evidence are independent.
    assert job.slot.hex not in str(report) and job.bundle.hex not in str(report)
    assert release_files.pointer(root, configuration.target.slot.hex) is None
    assert release_files.pointer(root, job.slot.hex) is None
    assert not (root / "bundles" / job.bundle.hex).exists()


def test_previous_bundle_is_restored_and_retained_at_unchanged_named_target(staging):
    artifact, configuration, root = staging
    prior = active_baseline(staging, different=True)
    job = passed(staging)
    assert job.report["identity"]["baseline"]["pointer"] == prior
    assert job.report["checks"]["restoration"]["file_count"] == 1
    assert (
        job.report["checks"]["restoration"]["root_digest"]
        != job.report["checks"]["candidate"]["root_digest"]
    )
    assert release_files.pointer(root, configuration.target.slot.hex) == prior
    assert (root / "bundles" / prior["release_id"]).exists()


@pytest.mark.parametrize(
    "change", ["configuration", "target", "report", "time", "baseline", "storage"]
)
def test_changed_evidence_or_destination_invalidates_prior_rehearsal(staging, monkeypatch, change):
    artifact, configuration, root = staging
    job = passed(staging)
    if change == "configuration":
        release_configuration.approve(
            artifact.pk, "another-target", configuration.pk, artifact.user.pk
        )
    elif change == "target":
        DeploymentTarget.objects.filter(pk=configuration.target_id).update(slot=uuid.uuid4())
    elif change == "report":
        RollbackRehearsal.objects.filter(pk=job.pk).update(report={})
    elif change == "time":
        RollbackRehearsal.objects.filter(pk=job.pk).update(finished_at=timezone.now())
    elif change == "storage":
        monkeypatch.setenv("TEMPO_RELEASE_ROOT", str(root / "another"))
    else:
        active_baseline(staging)
    assert not rehearsal.summary(artifact)["passed"]


def test_expired_and_new_failed_rehearsals_do_not_inherit_old_pass(staging, monkeypatch):
    artifact, _, _ = staging
    passed(staging)
    now = timezone.now
    monkeypatch.setattr(rehearsal.timezone, "now", lambda: now() + timedelta(hours=25))
    assert not rehearsal.summary(artifact)["passed"]
    monkeypatch.setattr(rehearsal.timezone, "now", now)
    enqueue(staging)
    job = rehearsal.claim()
    monkeypatch.setattr(
        rehearsal, "probe", lambda *args: (_ for _ in ()).throw(ValueError("offline"))
    )
    rehearsal.process(job)
    job.refresh_from_db()
    assert job.status == "failed"
    assert not rehearsal.summary(artifact)["passed"]
    assert release_files.pointer(staging[2], job.slot.hex) is None


@pytest.mark.parametrize("stage", ["prepare", "baseline", "candidate", "restore", "cleanup"])
def test_worker_crash_at_each_effect_boundary_revokes_private_target_and_fences_late_worker(
    staging, stage
):
    active_baseline(staging)
    enqueue(staging)
    job = rehearsal.claim()
    for name in ("prepare", "baseline", "candidate", "restore"):
        rehearsal.stage(job, name)
        if name == stage:
            break
    if stage == "cleanup":
        rehearsal.revoke(job)
    RollbackRehearsal.objects.filter(pk=job.pk).update(
        deadline=timezone.now() - timedelta(seconds=1)
    )
    rehearsal.recover()
    job.refresh_from_db()
    assert job.status == "interrupted"
    with pytest.raises(rehearsal.LeaseLost):
        rehearsal.stage(job, "candidate")
    assert release_files.pointer(staging[2], job.slot.hex) is None
    assert not (staging[2] / "bundles" / job.bundle.hex).exists()
    assert release_files.pointer(staging[2], staging[1].target.slot.hex)


def test_cleanup_failure_blocks_target_until_retry_succeeds(staging, monkeypatch):
    enqueue(staging)
    job = rehearsal.claim()
    rehearsal.stage(job, "prepare")
    rehearsal.stage(job, "candidate")
    RollbackRehearsal.objects.filter(pk=job.pk).update(
        deadline=timezone.now() - timedelta(seconds=1)
    )
    original = rehearsal.cleanup
    monkeypatch.setattr(rehearsal, "cleanup", lambda *_: (_ for _ in ()).throw(OSError("busy")))
    rehearsal.recover()
    job.refresh_from_db()
    assert job.status == "cleanup_pending"
    assert enqueue(staging).pk == job.pk
    monkeypatch.setattr(rehearsal, "cleanup", original)
    RollbackRehearsal.objects.filter(pk=job.pk).update(
        deadline=timezone.now() - timedelta(seconds=1)
    )
    rehearsal.recover()
    job.refresh_from_db()
    assert job.status == "interrupted"
    assert enqueue(staging).pk != job.pk


def test_named_target_change_while_candidate_is_served_interrupts_rehearsal(staging, monkeypatch):
    original = rehearsal.probe

    def probe(*args):
        report = original(*args)
        active_baseline(staging)
        return report

    monkeypatch.setattr(rehearsal, "probe", probe)
    enqueue(staging)
    job = rehearsal.claim()
    rehearsal.process(job)
    job.refresh_from_db()
    assert job.status == "failed"
    assert not rehearsal.summary(staging[0])["passed"]
    assert release_files.pointer(staging[2], job.slot.hex) is None


def test_idempotent_submission_authentication_csrf_and_scope(staging):
    artifact, configuration, _ = staging
    key = uuid.uuid4()
    first = enqueue(staging, key)
    assert enqueue(staging, key).pk == enqueue(staging).pk == first.pk
    with pytest.raises(ValueError, match="changed"):
        rehearsal.enqueue(artifact.pk, configuration.pk, "a" * 64, uuid.uuid4(), artifact.user.pk)
    url = artifact.url.replace("preview/", "rehearse/")
    client = Client()
    assert client.post(url).status_code == 302
    client.force_login(artifact.user)
    assert client.get(url).status_code == 405
    data = {
        "configuration_id": configuration.pk,
        "configuration_digest": configuration.digest,
        "request_key": uuid.uuid4(),
    }
    assert client.post(url, data).status_code == 302
    assert (
        client.post(url.replace(f"runs/{artifact.run_id}/", "runs/999999/"), data).status_code
        == 404
    )
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(artifact.user)
    assert csrf.post(url, data).status_code == 403
    response = client.post(url, {}, follow=True)
    assert b"could not start" in response.content and "no-store" in response["Cache-Control"]


def test_competing_requests_share_one_job_and_one_claim(staging):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    barrier = threading.Barrier(2)

    def submit():
        try:
            barrier.wait(timeout=5)
            return enqueue(staging).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert results[0] == results[1]

    def claim():
        try:
            barrier.wait(timeout=5)
            result = rehearsal.claim()
            return result.pk if result else None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert results.count(None) == 1


def test_expensive_verification_cannot_authorize_effects_after_deadline(staging, monkeypatch):
    enqueue(staging)
    job = rehearsal.claim()
    original = rehearsal.identity
    clock = timezone.now

    def expires(configuration):
        result = original(configuration)
        monkeypatch.setattr(rehearsal.timezone, "now", lambda: clock() + timedelta(minutes=5))
        return result

    monkeypatch.setattr(rehearsal, "identity", expires)
    with pytest.raises(rehearsal.LeaseLost):
        rehearsal.stage(job, "prepare")
    assert not (staging[2] / "bundles" / job.bundle.hex).exists()


def test_incomplete_health_evidence_and_restoration_failures_cannot_pass(staging, monkeypatch):
    active_baseline(staging, different=True)
    enqueue(staging)
    job = rehearsal.claim()
    original = rehearsal.probe
    calls = []

    def probe(*args):
        calls.append(args)
        report = original(*args)
        return report if len(calls) == 1 else {}

    monkeypatch.setattr(rehearsal, "probe", probe)
    rehearsal.process(job)
    job.refresh_from_db()
    assert job.status == "failed"
    assert not job.report
    assert release_files.pointer(staging[2], job.slot.hex) is None


def test_repeated_effects_reconcile_lost_responses_without_new_publication(staging):
    active_baseline(staging)
    enqueue(staging)
    job = rehearsal.claim()
    for name in ("prepare", "baseline", "candidate", "restore"):
        rehearsal.stage(job, name)
        before = release_files.pointer(staging[2], job.slot.hex)
        rehearsal.stage(job, name)
        assert release_files.pointer(staging[2], job.slot.hex) == before
    rehearsal.complete(job, error="Explicitly ended test")


def test_corrupt_active_bundle_is_rejected_before_rehearsal_intent(staging):
    prior = active_baseline(staging)
    (staging[2] / "bundles" / prior["release_id"] / "artifact.zip").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="bytes"):
        enqueue(staging)
    assert not RollbackRehearsal.objects.exists()
