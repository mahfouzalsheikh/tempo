import asyncio
import json
import os
import shlex
import subprocess

import httpx
import pytest

from tempo import validation_server
from tempo.config import ValidationConfig
from tempo.errors import ConfigError
from tempo.validation_sandbox import command_process, execution_config
from tempo.validation_server import run_command, stream_command


def test_docker_is_required_by_default_and_image_is_immutable(monkeypatch):
    monkeypatch.delenv("TEMPO_VALIDATION_BACKEND", raising=False)
    monkeypatch.delenv("TEMPO_VALIDATION_IMAGE", raising=False)
    with pytest.raises(ConfigError, match="immutable"):
        execution_config()
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", "some-image:latest")
    with pytest.raises(ConfigError, match="immutable"):
        execution_config()
    image = "sha256:" + "a" * 64
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", image)
    assert execution_config() == ("docker", image)
    old_digest = ValidationConfig().policy_digest
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", "sha256:" + "b" * 64)
    assert ValidationConfig().policy_digest != old_digest


@pytest.mark.asyncio
async def test_runner_rejects_image_mismatch_before_starting_command(tmp_path, monkeypatch):
    monkeypatch.setattr(validation_server, "ROOT", tmp_path)
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_TOKEN", "t" * 48)
    monkeypatch.setenv("TEMPO_VALIDATION_BACKEND", "docker")
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", "sha256:" + "a" * 64)
    task = tmp_path / "task"
    task.mkdir()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        response = await client.post("/run", headers={"Authorization": "Bearer " + "t" * 48}, json={
            "workspace": str(task), "command": "touch unexpected", "timeout_ms": 1000,
            "max_output_chars": 1000, "execution_image": "sha256:" + "b" * 64,
        })
    assert response.status_code == 409
    assert not (task / "unexpected").exists()


@pytest.fixture
def docker_sandbox(tmp_path, monkeypatch):
    image = os.getenv("TEMPO_TEST_VALIDATION_IMAGE")
    if not image:
        pytest.skip("Set TEMPO_TEST_VALIDATION_IMAGE to a local Tempo image ID for Docker probes")
    monkeypatch.setenv("TEMPO_VALIDATION_BACKEND", "docker")
    monkeypatch.setenv("TEMPO_VALIDATION_IMAGE", image)
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_TOKEN", "runner-secret-sentinel")
    monkeypatch.setenv("GITHUB_TOKEN", "tracker-secret-sentinel")
    task = tmp_path / "task"
    task.mkdir(mode=0o777)
    task.chmod(0o777)  # Only this disposable fixture is writable by container UID 10001.
    return task


@pytest.mark.asyncio
async def test_container_cannot_reach_sibling_credentials_daemon_or_network(docker_sandbox):
    sibling = docker_sandbox.parent / "sibling-secret"
    sibling.write_text("other-task-sentinel")
    probe = f"""
import json, os, pathlib, socket
assert os.getuid() == 10001
assert not pathlib.Path({str(sibling)!r}).exists()
assert not pathlib.Path('/run/tempo-host-codex').exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not os.getenv('TEMPO_VALIDATION_RUNNER_TOKEN')
assert not os.getenv('GITHUB_TOKEN')
assert not os.getenv('DOCKER_HOST')
assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
for directory in ['/app', '/data/database', '/home/tempo/.codex']:
    try:
        pathlib.Path(directory, 'unexpected').write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError(directory + ' is writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network access was allowed')
pathlib.Path('result.txt').write_text('workspace write allowed')
print('isolation probes passed')
"""
    result = await run_command(shlex.join(["python", "-c", probe]), docker_sandbox, 10000, 4000)
    assert result["exit_code"] == 0, result["output"]
    assert result["execution_image"] == os.environ["TEMPO_VALIDATION_IMAGE"]
    assert (docker_sandbox / "result.txt").read_text() == "workspace write allowed"


@pytest.mark.asyncio
async def test_watchdog_times_out_without_leaving_background_writer(docker_sandbox):
    result = await run_command(
        "(sleep 2; touch escaped) & sleep 30", docker_sandbox, 150, 1000,
    )
    assert result["exit_code"] in {124, 137}
    await asyncio.sleep(2)
    assert not (docker_sandbox / "escaped").exists()


@pytest.mark.asyncio
async def test_cancellation_removes_its_container(docker_sandbox):
    async def command():
        async for _ in stream_command("echo ready; sleep 30", docker_sandbox, 30000, 1000):
            ready.set()

    ready = asyncio.Event()
    worker = asyncio.create_task(command())
    await asyncio.wait_for(ready.wait(), 10)
    # Inspect just the job using our unique bind path; other jobs are not touched.
    container_ids = (await asyncio.to_thread(subprocess.check_output,
        ["docker", "ps", "-q", "--filter", "label=tempo.validation=true"], text=True,
    )).split()
    matches = []
    for container in container_ids:
        detail = json.loads(await asyncio.to_thread(
            subprocess.check_output, ["docker", "inspect", container],
        ))[0]
        if any(mount["Source"] == str(docker_sandbox) for mount in detail["Mounts"]):
            matches.append(container)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert len(matches) == 1
    inspected = await asyncio.to_thread(
        subprocess.run, ["docker", "inspect", matches[0]], capture_output=True,
    )
    assert inspected.returncode != 0


@pytest.mark.asyncio
async def test_concurrent_commands_get_separate_workspaces(docker_sandbox):
    other = docker_sandbox.parent / "other"
    other.mkdir(mode=0o777)
    other.chmod(0o777)
    results = await asyncio.gather(
        run_command("echo first > marker; sleep .1; cat marker", docker_sandbox, 5000, 1000),
        run_command("echo second > marker; sleep .1; cat marker", other, 5000, 1000),
    )
    assert [result["exit_code"] for result in results] == [0, 0]
    assert [result["output"].strip() for result in results] == ["first", "second"]


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_emit_a_passing_result(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    monkeypatch.setenv("TEMPO_VALIDATION_BACKEND", "process")

    @asynccontextmanager
    async def fail_cleanup(command, workspace, timeout_ms):
        async with command_process(command, workspace, timeout_ms) as process:
            yield process
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(validation_server, "command_process", fail_cleanup)
    events = []
    with pytest.raises(RuntimeError, match="cleanup unavailable"):
        async for event in stream_command("echo checked", tmp_path, 1000, 1000):
            events.append(event)
    assert not any(event["type"] == "result" for event in events)
