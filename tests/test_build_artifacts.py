import asyncio
import copy
import hashlib
import io
import json
import os
import shlex
import shutil
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from asgiref.sync import sync_to_async
from django.test import Client
from test_product_execution import context, enqueue
from test_product_execution import factory as product_factory  # noqa: F401

from tempo.build_artifacts import clear_output, package_directory, verify_artifact
from tempo.build_profiles import MINI_APP, build_profile
from tempo.errors import CodexError, ConfigError
from tempo.intake import IntakeConflict
from tempo.orchestrator import Orchestrator
from tempo.product_execution import compile_product, restore_product
from tempo.run_snapshot import snapshot_digest
from tempo_web.models import AgentRun, BuildArtifact, RunCheckpoint

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def factory(product_factory):  # noqa: F811 - imported shared pytest fixture
    return product_factory


def test_build_profile_augments_checks_without_changing_source_or_legacy_snapshot(tmp_path):
    saved = context(tmp_path)
    original = copy.deepcopy(saved)
    legacy = compile_product(saved)
    saved.update(schema=2, build_profile=build_profile(MINI_APP["id"]))
    built = compile_product(saved)
    assert compile_product(original) == legacy
    assert (
        built["config"]["validation"]["required_checks"][:1]
        == legacy["config"]["validation"]["required_checks"]
    )
    assert len(built["config"]["validation"]["required_checks"]) == 3
    assert original["source_snapshot"] == saved["source_snapshot"]
    saved["build_profile"]["prepare"] = "unreviewed-command"
    with pytest.raises(IntakeConflict):
        compile_product(saved)


def test_build_target_can_supply_missing_checks_but_not_weaken_disabled_policy(tmp_path):
    saved = context(tmp_path)
    saved["source_snapshot"]["config"]["validation"]["required_checks"] = []
    saved["source_digest"] = snapshot_digest(saved["source_snapshot"])
    with pytest.raises(IntakeConflict):
        compile_product(saved)
    saved.update(schema=2, build_profile=build_profile(MINI_APP["id"]))
    assert len(compile_product(saved)["config"]["validation"]["required_checks"]) == 2
    saved["source_snapshot"]["config"]["validation"]["enabled"] = False
    saved["source_digest"] = snapshot_digest(saved["source_snapshot"])
    with pytest.raises(IntakeConflict):
        compile_product(saved)


def test_profile_is_frozen_and_cannot_change_on_replayed_launch(factory):
    run = enqueue(factory, build_target=MINI_APP["id"])
    assert restore_product(run)["build_profile"] == MINI_APP
    assert enqueue(factory, build_target=MINI_APP["id"]).pk == run.pk
    with pytest.raises(IntakeConflict, match="different execution settings"):
        enqueue(factory)
    run.product_snapshot["build_profile"]["output"] = "../outside"
    run.product_snapshot_digest = snapshot_digest(run.product_snapshot)
    with pytest.raises(ConfigError):
        restore_product(run)


def output_fixture(tmp_path):
    directory = tmp_path / "mini-app" / "dist"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<h1>Built</h1>")
    return directory


def test_archive_is_deterministic_and_survives_workspace_removal(tmp_path):
    output_fixture(tmp_path)
    bundle = package_directory(tmp_path, "mini-app/dist")
    assert bundle == package_directory(tmp_path, "mini-app/dist")
    clear_output(tmp_path, "mini-app/dist")
    with zipfile.ZipFile(io.BytesIO(bundle["data"])) as archive:
        assert archive.read("index.html") == b"<h1>Built</h1>"
    assert bundle["digest"] == hashlib.sha256(bundle["data"]).hexdigest()
    assert bundle["files"][0]["sha256"] == hashlib.sha256(b"<h1>Built</h1>").hexdigest()


@pytest.mark.parametrize(
    "bad",
    [
        "parent_link",
        "directory_link",
        "file_link",
        "hardlink",
        "fifo",
        "missing_index",
        "size",
        "count",
    ],
)
def test_unsafe_or_oversized_outputs_are_rejected(tmp_path, monkeypatch, bad):
    import tempo.build_artifacts as artifacts

    directory = output_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("private")
    if bad == "parent_link":
        (tmp_path / "mini-app").rename(tmp_path / "original")
        (tmp_path / "mini-app").symlink_to(tmp_path / "original", target_is_directory=True)
    elif bad == "directory_link":
        (directory / "linked").symlink_to(tmp_path, target_is_directory=True)
    elif bad == "file_link":
        (directory / "leak").symlink_to(outside)
    elif bad == "hardlink":
        os.link(outside, directory / "leak")
    elif bad == "fifo":
        os.mkfifo(directory / "fifo")
    elif bad == "missing_index":
        (directory / "index.html").unlink()
    elif bad == "size":
        monkeypatch.setattr(artifacts, "MAX_BYTES", 5)
    else:
        monkeypatch.setattr(artifacts, "MAX_FILES", 0)
    with pytest.raises(CodexError):
        package_directory(tmp_path, "mini-app/dist")
    assert outside.read_text() == "private"


def test_cleanup_rejects_output_symlink_without_touching_target(tmp_path):
    directory = output_fixture(tmp_path)
    original = tmp_path / "keep"
    directory.rename(original)
    directory.symlink_to(original, target_is_directory=True)
    with pytest.raises(CodexError):
        clear_output(tmp_path, "mini-app/dist")
    assert (original / "index.html").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "checks", "cleanup", "capture", "source", "lease"])
async def test_full_candidate_build_keeps_only_verified_artifacts(factory, monkeypatch, failure):
    from tempo import build_profiles
    from tempo.agent_runtime import providers

    package = {
        "scripts": {
            "test": "node -e \"console.log('Unit checks passed')\"",
            "build": "node build.cjs",
            "validate:dist": "node check.cjs",
        }
    }
    build = (
        "const fs=require('fs');fs.mkdirSync('dist',{recursive:true});"
        "fs.writeFileSync('dist/index.html','<h1>Built</h1>');"
    )
    check = (
        "const fs=require('fs');"
        "if(fs.readFileSync('dist/index.html','utf8')!=='<h1>Built</h1>')process.exit(1);"
    )
    if failure == "checks":
        check += "process.exit(1);"
    if failure == "capture":
        build += "fs.symlinkSync('../package.json','dist/unsafe');"
    files = {
        "mini-app/package.json": json.dumps(package),
        "mini-app/build.cjs": build,
        "mini-app/check.cjs": check,
        "left.txt": "left",
        "right.txt": "right",
    }
    script = (
        f"from pathlib import Path\nfiles={files!r}\nfor p,s in files.items():\n"
        " f=Path(p);f.parent.mkdir(parents=True,exist_ok=True);f.write_text(s)\n"
    )
    factory.config.hooks.after_create += "\npython -c " + shlex.quote(script)
    factory.config.hooks.after_create += (
        '\nprintf "mini-app/dist/\\n" >> .gitignore\ngit add .\ngit commit -m Fixture'
    )
    # Cleanup can remove generated files; the captured artifact must retain validated bytes.
    factory.config.validation.cleanup_command = "rm -rf mini-app/dist"
    if failure == "cleanup":
        factory.config.validation.cleanup_command += "\nexit 1"
    factory.source.path.write_text(
        "---\n"
        + yaml.safe_dump(factory.config.model_dump(mode="json", by_alias=True))
        + "---\nFixture"
    )
    controller = Orchestrator(str(factory.source.path))
    await controller.store.initialize()
    await controller._apply_config(controller.store.current()[1])
    factory.store = controller.persistence
    run = await sync_to_async(enqueue)(factory, build_target=MINI_APP["id"])

    async def prepare(profile, path, on_event):
        assert profile == MINI_APP
        from datetime import timedelta

        from tempo.domain import utcnow

        entry = controller.running[f"product-plan:{factory.plan.pk}"]
        entry.session.last_codex_timestamp = utcnow() - timedelta(seconds=300)
        await controller._reconcile(controller.store.current()[1])
        assert not entry.task.cancelling()
        # Simulate a stale agent-created dist tree. It must be removed before final checks.
        (path / "mini-app/dist").mkdir(exist_ok=True)
        (path / "mini-app/dist/stale.txt").write_text("stale")
        if failure == "source":
            (path / "left.txt").write_text("modified source")
        if failure == "lease":
            await AgentRun.objects.filter(pk=run.pk).aupdate(lease_token="replacement-owner")

    monkeypatch.setattr(build_profiles, "prepare_build", prepare)

    def runtime(_config, _profile, _manager, _tracker, on_event, *_args, **_kwargs):
        class Runtime:
            async def start_session(self, workspace, **kwargs):
                return SimpleNamespace(resumed=False)

            async def run_turn(self, session, prompt, issue):
                await on_event({"event": "turn/completed", "usage": {"total_tokens": 1}})

            async def stop_session(self, session):
                pass

        return Runtime()

    monkeypatch.setattr(providers, "create_runtime", runtime)
    issue = await controller.persistence.queued_issue(run.pk)
    token = await controller.persistence.claim_run(run.pk, "build-worker")
    controller._dispatch_locked(issue, 0, run_record_id=run.pk, lease_token=token)
    task = controller.running[issue.id].task
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 25)
        await controller._worker_finished(issue.id, task)
        await run.arefresh_from_db()
        if failure:
            assert not await BuildArtifact.objects.filter(run=run).aexists()
            assert not await RunCheckpoint.objects.filter(
                run=run, kind="product_candidate"
            ).aexists()
            if failure != "lease":
                assert run.status == "failed", run.error
            return
        assert run.status == "succeeded", run.error
        artifact = await BuildArtifact.objects.aget(run=run)
        assert artifact.manifest["readiness"]["deployment_ready"] is False
        assert all(row["status"] == "unverified" for row in artifact.manifest["acceptance"])
        with zipfile.ZipFile(io.BytesIO(verify_artifact(artifact))) as archive:
            assert archive.namelist() == ["index.html"]
            assert archive.read("index.html") == b"<h1>Built</h1>"
        from pathlib import Path

        assert not (Path(run.workspace_path) / "mini-app/dist").exists()
        await asyncio.to_thread(shutil.rmtree, run.workspace_path)

        def browser_checks():
            client = Client()
            url = f"/ideas/{factory.product.pk}/runs/{run.pk}/artifacts/{artifact.pk}/archive/"
            assert client.get(url).status_code == 302
            client.force_login(factory.user)
            response = client.get(url)
            assert response.status_code == 200
            assert response.content == bytes(artifact.data)
            assert response["Content-Disposition"].startswith("attachment;")
            assert response["Content-Security-Policy"].startswith("sandbox;")
            manifest = client.get(url.replace("/archive/", "/manifest/"))
            assert manifest.json() == artifact.manifest
            assert (
                client.get(url.replace(f"/ideas/{factory.product.pk}/", "/ideas/999/")).status_code
                == 404
            )
            page = client.get(f"/ideas/{factory.product.pk}/")
            assert b"Download build ZIP" in page.content
            BuildArtifact.objects.filter(pk=artifact.pk).update(data=b"corrupted")
            assert client.get(url).status_code == 409

        await sync_to_async(browser_checks)()
    finally:
        await controller.stop()


def test_setup_makes_build_checks_explicit_without_enabling_issue_dispatch(factory, monkeypatch):
    from tempo_web import product_views

    factory.store.execution_snapshot["config"]["validation"]["required_checks"] = []
    monkeypatch.setattr(
        product_views, "get_orchestrator", lambda: SimpleNamespace(persistence=factory.store)
    )
    assert product_views.setup(factory.product)["blocked"]
    info = product_views.setup(factory.product, MINI_APP["id"])
    assert not info["blocked"] and len(info["checks"]) == 2
    assert factory.store.execution_snapshot["config"]["validation"]["required_checks"] == []
    client = Client(enforce_csrf_checks=True)
    client.force_login(factory.user)
    url = f"/ideas/{factory.product.pk}/execute/?build_target={MINI_APP['id']}"
    page = client.get(url)
    assert page.status_code == 200 and b"Mini-app unit tests" in page.content
    assert client.post(url, {}).status_code == 403


@pytest.mark.asyncio
async def test_artifact_writes_are_idempotent_and_fenced_by_run_lease(factory, tmp_path):
    from tempo.errors import LeaseLostError

    run = await sync_to_async(enqueue)(factory, build_target=MINI_APP["id"])
    token = await factory.store.claim_run(run.pk, "artifact-writer")
    output_fixture(tmp_path)
    bundle = package_directory(tmp_path, "mini-app/dist")
    manifest = {"archive": {"sha256": bundle["digest"], "size": bundle["size"], "format": "zip"}}
    first = await factory.store.save_build_artifact(run.pk, bundle, manifest, lease_token=token)
    assert (
        await factory.store.save_build_artifact(run.pk, bundle, manifest, lease_token=token)
        == first
    )
    assert await BuildArtifact.objects.filter(run=run).acount() == 1
    await AgentRun.objects.filter(pk=run.pk).aupdate(lease_token="replacement-owner")
    with pytest.raises(LeaseLostError):
        await factory.store.save_build_artifact(run.pk, bundle, manifest, lease_token=token)


@pytest.mark.asyncio
async def test_preparation_has_no_tracker_or_model_credentials(tmp_path, monkeypatch):
    from tempo import workload
    from tempo.build_profiles import prepare_build

    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-project-code")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-project-code")
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_TOKEN", "must-not-reach-project-code")
    closed, events = [], []

    async def start(command, path, environment, **kwargs):
        assert "ONNXRUNTIME_NODE_INSTALL=skip npm ci" in command
        assert not {"GITHUB_TOKEN", "OPENAI_API_KEY", "TEMPO_VALIDATION_RUNNER_TOKEN"} & set(
            environment
        )
        assert kwargs["kind"] == "build-preparation"
        output = asyncio.StreamReader()
        output.feed_data(b"prepared")
        output.feed_eof()

        async def wait():
            return 0

        return SimpleNamespace(stdout=output, returncode=0, wait=wait)

    async def stop(process):
        closed.append(True)

    async def on_event(event):
        events.append(event)

    monkeypatch.setattr(workload, "start_workload", start)
    monkeypatch.setattr(workload, "stop_workload", stop)
    await prepare_build(MINI_APP, tmp_path, on_event)
    assert closed and events[-1]["event"] == "build_preparation_completed"
