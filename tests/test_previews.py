import hashlib
import io
import json
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest
from django.db import connection, connections
from django.test import Client
from test_product_execution import enqueue
from test_product_execution import factory as product_factory  # noqa: F401

from tempo import preview_files
from tempo.build_artifacts import artifact_manifest, package_directory
from tempo.build_profiles import MINI_APP
from tempo.config import ServiceConfig
from tempo.domain import utcnow
from tempo.preview_server import PreviewHandler
from tempo.run_snapshot import snapshot_digest
from tempo_web.models import BuildArtifact, PreviewDeployment, RunCheckpoint, ValidationAttempt
from tempo_web.preview_views import details

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def bundle(tmp_path):
    output = tmp_path / "dist"
    output.mkdir()
    (output / "index.html").write_text('<script type="module" src="/app.js"></script>')
    (output / "app.js").write_text('document.body.append("Saved build");')
    return package_directory(tmp_path, "dist")


@pytest.fixture
def artifact(product_factory, bundle, tmp_path, monkeypatch):  # noqa: F811
    factory = product_factory
    monkeypatch.setenv("TEMPO_PREVIEW_ROOT", str(tmp_path / "previews"))
    run = enqueue(factory, build_target=MINI_APP["id"])
    policy = ServiceConfig.model_validate(run.execution_snapshot["config"]).validation
    check = ValidationAttempt.objects.create(
        run=run,
        status="passed",
        started_at=utcnow(),
        workspace_fingerprint="f" * 64,
        required_check_ids=[c.id for c in policy.required_checks],
        policy_digest=policy.policy_digest,
    )
    plan = run.execution_plan
    candidate = {
        "source_sha": "a" * 40,
        "snapshot_digest": run.snapshot_digest,
        "brief_digest": plan.brief_revision.digest,
        "plan_digest": plan.digest,
        "plan_id": plan.pk,
        "validation_record_id": check.pk,
        "policy_digest": check.policy_digest,
        "required_check_ids": check.required_check_ids,
        "workspace_fingerprint": "f" * 64,
    }
    manifest = artifact_manifest(run.product_snapshot, candidate, bundle)
    saved = BuildArtifact.objects.create(
        run=run,
        digest=bundle["digest"],
        size=bundle["size"],
        data=bundle["data"],
        manifest=manifest,
        manifest_digest=snapshot_digest(manifest),
    )
    candidate["artifact"] = {
        "id": saved.pk,
        "sha256": saved.digest,
        "size": saved.size,
        "manifest_digest": saved.manifest_digest,
    }
    RunCheckpoint.objects.create(
        run=run,
        kind="product_candidate",
        payload=candidate,
        sequence=1,
        idempotency_key="candidate",
    )
    run.status, run.phase = "succeeded", "CandidateChecksPassed"
    run.save(update_fields=["status", "phase"])
    saved.url = f"/ideas/{factory.product.pk}/runs/{run.pk}/artifacts/{saved.pk}/preview/"
    saved.user = factory.user
    saved.root = tmp_path / "previews"
    return saved


def test_preview_launch_authentication_scope_and_csrf(artifact):
    client = Client(enforce_csrf_checks=True)
    assert client.get(artifact.url + "start/").status_code == 302
    client.force_login(artifact.user)
    assert client.get(artifact.url + "start/").status_code == 405
    assert client.post(artifact.url + "start/").status_code == 403
    client = Client()
    client.force_login(artifact.user)
    # PKs do not reset on every PostgreSQL test.
    wrong = "/ideas/99999/" + artifact.url.split("/", 3)[3]
    assert client.post(wrong + "start/").status_code == 404
    assert PreviewDeployment.objects.count() == 0


def test_idea_pages_with_preview_links_are_private_and_not_cached(artifact):
    client = Client()
    idea = artifact.url.split("/runs/", 1)[0] + "/"
    denied = client.get(idea)
    assert denied.status_code == 302 and "no-store" in denied["Cache-Control"]
    client.force_login(artifact.user)
    assert client.post(artifact.url + "start/").status_code == 302
    response = client.get(idea)
    assert response.status_code == 200 and b"Open preview" in response.content
    assert "no-store" in response["Cache-Control"]
    assert {"Cookie", "Authorization"} <= set(response["Vary"].split(", "))


def test_launch_replay_restart_repair_stop_and_expiration(artifact):
    client = Client()
    client.force_login(artifact.user)
    assert client.post(artifact.url + "start/").status_code == 302
    first = PreviewDeployment.objects.get(artifact=artifact)
    assert (artifact.root / first.token.hex / "artifact.zip").read_bytes() == bytes(artifact.data)
    assert details(artifact.pk)["active"]
    assert client.post(artifact.url + "start/").status_code == 302
    assert PreviewDeployment.objects.get(artifact=artifact).token == first.token
    # No controller/workspace or process-local cache is involved in recovery.
    shutil.rmtree(artifact.root / first.token.hex)
    assert not details(artifact.pk)["active"]
    assert client.post(artifact.url + "start/").status_code == 302
    second = PreviewDeployment.objects.get(artifact=artifact)
    assert second.token != first.token
    location = artifact.root / second.token.hex / "metadata.json"
    document = json.loads(location.read_text())
    document["expires_at"] = time.time() - 1
    location.write_text(json.dumps(document))
    assert not details(artifact.pk)["active"]
    assert client.post(artifact.url + "start/").status_code == 302
    third = PreviewDeployment.objects.get(artifact=artifact)
    assert third.token != second.token
    assert client.post(artifact.url + "stop/").status_code == 302
    assert client.post(artifact.url + "stop/").status_code == 302
    assert not details(artifact.pk)["active"]
    assert not (artifact.root / third.token.hex).exists()
    assert BuildArtifact.objects.get(pk=artifact.pk).data == artifact.data


@pytest.mark.parametrize("failure", ["bytes", "manifest", "checkpoint", "validation", "phase"])
def test_preview_requires_intact_build_and_passed_evidence(artifact, failure):
    if failure == "bytes":
        BuildArtifact.objects.filter(pk=artifact.pk).update(data=b"corrupt")
    elif failure == "manifest":
        BuildArtifact.objects.filter(pk=artifact.pk).update(manifest={})
    elif failure == "checkpoint":
        artifact.run.checkpoints.all().delete()
    elif failure == "validation":
        ValidationAttempt.objects.filter(run=artifact.run).update(status="failed")
    else:
        artifact.run.phase = "Running"
        artifact.run.save(update_fields=["phase"])
    client = Client()
    client.force_login(artifact.user)
    assert client.post(artifact.url + "start/").status_code == 409
    assert PreviewDeployment.objects.count() == 0
    assert not artifact.root.exists()


def test_concurrent_preview_starts_use_one_capability(artifact):
    if connection.vendor != "postgresql":
        pytest.skip("Preview launch row locking requires PostgreSQL")
    clients = [Client(), Client()]
    for client in clients:
        client.force_login(artifact.user)
    barrier = threading.Barrier(2)

    def start(client):
        try:
            barrier.wait(timeout=10)
            return client.post(artifact.url + "start/").status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(start, clients)) == [302, 302]
    assert PreviewDeployment.objects.count() == 1
    assert len(list(artifact.root.iterdir())) == 1


@pytest.fixture
def serving(tmp_path, bundle):
    token = uuid.uuid4().hex
    preview_files.publish(
        tmp_path,
        token,
        bundle["data"],
        bundle["files"],
        bundle["digest"],
        time.time() + 3600,
    )
    server = HTTPServer(("127.0.0.1", 0), PreviewHandler)
    server.root, server.public_port = tmp_path, 8031
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path="/", host=None, method="GET", headers=None):
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request(
            method, path, headers={"Host": host or f"{token}.localhost:8031", **(headers or {})}
        )
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    yield request, token, tmp_path
    server.shutdown()
    server.server_close()
    thread.join()


def test_static_serving_routes_headers_methods_and_restart(serving, bundle):
    request, _token, _root = serving
    status, headers, data = request()
    assert status == 200 and data == zipfile.ZipFile(io.BytesIO(bundle["data"])).read("index.html")
    assert headers["Content-Security-Policy"].startswith(
        "sandbox allow-scripts allow-same-origin allow-downloads;"
    )
    assert "worker-src 'self' blob:" in headers["Content-Security-Policy"]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in headers and "Set-Cookie" not in headers
    assert headers["Cache-Control"] == "no-store"
    assert request("/app.js")[0] == 200
    assert request("/some/route")[2] == data
    assert request("/missing.js")[0] == 404
    assert request("/", method="HEAD")[2] == b""
    assert request("/", method="POST")[0] == 501
    assert request("/app.js", headers={"Service-Worker": "script"})[0] == 403
    assert request("/app.js", headers={"Sec-Fetch-Dest": "serviceworker"})[0] == 403


@pytest.mark.parametrize(
    "host",
    ["localhost:8031", "localhost:8001", "tempo:8000", "x.localhost:8031", "a" * 32 + ".evil:8031"],
)
def test_preview_requires_its_random_host(serving, host):
    assert serving[0](host=host)[0] == 404


@pytest.mark.parametrize(
    "path", ["/../metadata.json", "/%2e%2e/artifact.zip", "/.env", "/.git/config"]
)
def test_preview_cannot_read_serving_metadata_or_private_paths(serving, path):
    assert serving[0](path)[0] == 404


def test_corrupted_expired_and_revoked_previews_fail_closed(serving):
    request, token, root = serving
    location = root / token / "artifact.zip"
    location.write_bytes(b"damaged")
    assert request()[0] == 404
    document = json.loads((root / token / "metadata.json").read_text())
    document["expires_at"] = time.time() - 1
    (root / token / "metadata.json").write_text(json.dumps(document))
    assert request()[0] == 404
    preview_files.remove(root, token)
    assert request()[0] == 404


@pytest.mark.parametrize("name", ["../index.html", "/index.html", ".env", "a\\b", "a:b"])
def test_hostile_archives_are_never_published(tmp_path, name):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, b"x")
    data = output.getvalue()
    files = [{"path": name, "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}]
    with pytest.raises(ValueError):
        preview_files.publish(
            tmp_path,
            uuid.uuid4().hex,
            data,
            files,
            hashlib.sha256(data).hexdigest(),
            time.time() + 60,
        )
    assert not list(tmp_path.iterdir())


def test_preview_reclaims_expired_files_and_abandoned_staging(tmp_path, bundle):
    old, fresh = uuid.uuid4().hex, uuid.uuid4().hex
    preview_files.publish(
        tmp_path, old, bundle["data"], bundle["files"], bundle["digest"], time.time() - 1
    )
    staging = tmp_path / ".preview-abandoned"
    staging.mkdir()
    import os

    os.utime(staging, (time.time() - 90000, time.time() - 90000))
    recent = tmp_path / ".preview-in-progress"
    recent.mkdir()
    preview_files.publish(
        tmp_path, fresh, bundle["data"], bundle["files"], bundle["digest"], time.time() + 60
    )
    assert not (tmp_path / old).exists() and not staging.exists()
    assert recent.exists() and preview_files.available(tmp_path, fresh, bundle["digest"])


@pytest.mark.parametrize("case", ["symlink", "duplicate", "inventory", "size"])
def test_archive_metadata_cannot_bypass_preview_validation(tmp_path, case):
    output = io.BytesIO()
    member = zipfile.ZipInfo("index.html")
    if case == "symlink":
        member.external_attr = 0o120777 << 16
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(member, b"x")
        if case == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("index.html", b"x")
    files = [{"path": "index.html", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}]
    if case == "inventory":
        files[0]["sha256"] = "0" * 64
    if case == "size":
        files[0]["size"] = 2
    data = output.getvalue()
    with pytest.raises(ValueError):
        preview_files.publish(
            tmp_path,
            uuid.uuid4().hex,
            data,
            files,
            hashlib.sha256(data).hexdigest(),
            time.time() + 60,
        )
    assert not list(tmp_path.iterdir())
