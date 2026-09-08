import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from test_product_execution import enqueue
from test_product_execution import factory as product_factory

from tempo import validation_sandbox, validation_server
from tempo.errors import ConfigError
from tempo.product_execution import compile_product
from tempo.run_snapshot import snapshot_digest
from tempo.validation_images import allowed_images, retained_images

factory = product_factory
CURRENT, PREVIOUS = "sha256:" + "a" * 64, "sha256:" + "b" * 64
TOKEN = "test-runner-" + "x" * 48


@pytest.mark.parametrize("value", ["{}", '"image"', '[null]', '[true]', '["image:latest"]',
                                       '["sha256:bad"]', "[", " " * 20001,
                                       json.dumps([PREVIOUS] * 257)])
def test_retained_image_configuration_rejects_mutable_malformed_and_unbounded_lists(
    monkeypatch, value,
):
    monkeypatch.setenv("TEMPO_VALIDATION_RETAINED_IMAGES", value)
    with pytest.raises(ConfigError):
        allowed_images(CURRENT)


def test_default_image_and_deduplicated_retained_ids_are_allowed(monkeypatch):
    monkeypatch.delenv("TEMPO_VALIDATION_RETAINED_IMAGES", raising=False)
    assert allowed_images(CURRENT) == {CURRENT}
    monkeypatch.setenv("TEMPO_VALIDATION_RETAINED_IMAGES", json.dumps([PREVIOUS, PREVIOUS]))
    assert allowed_images(CURRENT) == {CURRENT, PREVIOUS}


@pytest.mark.django_db(transaction=True)
def test_catalog_verifies_both_product_and_execution_contracts_without_mutating_them(factory):
    run = enqueue(factory)
    context = copy.deepcopy(run.product_snapshot)
    context["source_snapshot"]["execution"]["environment"]["TEMPO_VALIDATION_IMAGE"] = PREVIOUS
    context["source_digest"] = snapshot_digest(context["source_snapshot"])
    run.product_snapshot, run.product_snapshot_digest = context, snapshot_digest(context)
    run.execution_snapshot = compile_product(context)
    run.snapshot_digest = snapshot_digest(run.execution_snapshot)
    run.save()
    before = copy.deepcopy(run.execution_snapshot)
    assert retained_images()["images"] == [PREVIOUS]
    run.refresh_from_db()
    assert run.execution_snapshot == before
    run.product_snapshot_digest = "0" * 64
    run.save(update_fields=["product_snapshot_digest"])
    catalog = retained_images()
    assert catalog["images"] == []
    assert catalog["skipped"] == [{"kind": "run", "id": run.pk, "reason": "snapshot_invalid"}]


@pytest.mark.django_db(transaction=True)
def test_catalog_includes_workflow_override_and_skips_invalid_digest(factory):
    version = factory.store.workflow_version_id
    from tempo_web.models import WorkflowVersion

    row = WorkflowVersion.objects.get(pk=version)
    row.execution_snapshot["config"]["validation"]["runner_image"] = PREVIOUS
    row.execution_snapshot["execution"]["environment"]["TEMPO_VALIDATION_IMAGE"] = CURRENT
    row.checksum = snapshot_digest(row.execution_snapshot)
    row.save(update_fields=["execution_snapshot", "checksum"])
    assert retained_images()["images"] == [PREVIOUS]
    row.checksum = "0" * 64
    row.save(update_fields=["checksum"])
    assert retained_images()["images"] == []


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPO_VALIDATION_BACKEND", "docker")
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", CURRENT)
    monkeypatch.setenv("TEMPO_VALIDATION_RETAINED_IMAGES", json.dumps([PREVIOUS]))
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_TOKEN", TOKEN)
    monkeypatch.setattr(validation_server, "ROOT", tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    return task


@pytest.mark.parametrize("endpoint", ["/run", "/run-stream"])
async def test_concurrent_requests_keep_their_exact_image_through_launch_and_result(
    runner, monkeypatch, endpoint,
):
    processes, calls, removed = [], [], []

    async def create(*args, **kwargs):
        assert args[0] == "docker"
        if args[1:3] == ("image", "inspect"):
            async def communicate():
                return (args[-1] + "\n").encode(), b""
            return SimpleNamespace(communicate=communicate, returncode=0)
        calls.append(args)
        assert "--pull=never" in args and "--network=none" in args and "--read-only" in args
        image = args[args.index("--entrypoint") + 2]
        stdout = asyncio.StreamReader()

        async def wait():
            return 0

        process = SimpleNamespace(stdout=stdout, wait=wait, returncode=0, expected=image)
        processes.append(process)
        if len(processes) == 2:
            for item in processes:
                item.stdout.feed_data(item.expected.encode())
                item.stdout.feed_eof()
        return process

    async def remove(name):
        removed.append(name)

    monkeypatch.setattr(validation_sandbox.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(validation_sandbox, "remove_container", remove)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        async def request(image):
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {TOKEN}"},
                                         json={"workspace": str(runner), "command": "true",
                                               "timeout_ms": 1000, "max_output_chars": 1000,
                                               "execution_image": image})
            assert response.status_code == 200
            result = json.loads(response.text.splitlines()[-1])
            assert result["execution_image"] == image and result["output"] == image

        await asyncio.wait_for(asyncio.gather(request(CURRENT), request(PREVIOUS)), 5)
    assert len(calls) == 2 and len(set(removed)) == 2
    assert validation_sandbox.execution_config() == ("docker", CURRENT)


@pytest.mark.parametrize("requested,available,error", [
    (PREVIOUS, False, "execution_image_unavailable"),
    ("sha256:" + "c" * 64, True, "execution_image_mismatch"),
    ("some-image:latest", True, "execution_image_mismatch"),
    (None, True, "execution_image_mismatch"),
    ([PREVIOUS], True, "execution_image_mismatch"),
])
async def test_unavailable_and_unapproved_images_fail_before_execution(
    runner, monkeypatch, requested, available, error,
):
    calls = []

    async def create(*args, **kwargs):
        calls.append(args)
        assert args[1:3] == ("image", "inspect")

        async def communicate():
            return b"", b""

        return SimpleNamespace(returncode=1, communicate=communicate)

    monkeypatch.setattr(validation_sandbox.asyncio, "create_subprocess_exec", create)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        response = await client.post("/run", headers={"Authorization": f"Bearer {TOKEN}"}, json={
            "workspace": str(runner), "command": "touch unexpected", "timeout_ms": 1000,
            "max_output_chars": 1000, "execution_image": requested,
        })
    assert response.status_code == 409 and response.json() == {"error": error}
    assert len(calls) == (0 if available else 1)
    assert not (runner / "unexpected").exists()


@pytest.mark.parametrize("failure,status", [("daemon", 503), ("timeout", 503), ("alias", 409)])
async def test_inspection_failures_never_start_commands_or_leak_daemon_errors(
    runner, monkeypatch, failure, status,
):
    stopped = []

    async def create(*args, **kwargs):
        assert args[1:3] == ("image", "inspect")
        if failure == "daemon":
            raise OSError("private daemon error")

        async def communicate():
            if failure == "timeout":
                raise TimeoutError("private daemon timeout")
            return CURRENT.encode(), b""

        return SimpleNamespace(returncode=0, communicate=communicate)

    async def stop(process):
        stopped.append(process)

    monkeypatch.setattr(validation_sandbox.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(validation_sandbox, "stop_process_group", stop)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        response = await client.post("/run-stream", headers={"Authorization": f"Bearer {TOKEN}"},
                                     json={"workspace": str(runner), "command": "true",
                                           "timeout_ms": 1000, "max_output_chars": 1000,
                                           "execution_image": PREVIOUS})
    assert response.status_code == status and "private daemon" not in response.text
    assert len(stopped) == (1 if failure == "timeout" else 0)


async def test_invalid_catalog_fails_health_and_requests_closed(runner, monkeypatch):
    monkeypatch.setenv("TEMPO_VALIDATION_RETAINED_IMAGES", '["mutable:tag"]')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        assert (await client.get("/healthz")).status_code == 503
        response = await client.post("/run", headers={"Authorization": f"Bearer {TOKEN}"}, json={
            "workspace": str(runner), "command": "true", "timeout_ms": 1000,
            "max_output_chars": 1000, "execution_image": CURRENT,
        })
        assert response.status_code == 503
