import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from tempo import validation_server
from tempo.config import HooksConfig, ValidationConfig
from tempo.errors import ConfigError
from tempo.validation import ProjectValidator
from tempo.workspace import WorkspaceManager

TOKEN = "test-runner-" + "x" * 48


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPO_VALIDATION_BACKEND", "process")
    monkeypatch.setenv("TEMPO_VALIDATION_RUNNER_TOKEN", TOKEN)
    monkeypatch.setattr(validation_server, "ROOT", tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/run", "/run-stream"])
async def test_unauthenticated_jobs_are_rejected_before_reading_body(runner, path):
    async def receive():
        raise AssertionError("unauthorized job body was read")

    messages = []

    async def send(message):
        messages.append(message)

    for headers in [[], [(b"authorization", b"Bearer wrong")],
                    [(b"authorization", f"Bearer {TOKEN}".encode())] * 2]:
        messages.clear()
        await validation_server.application(
            {"type": "http", "method": "POST", "path": path, "headers": headers}, receive, send,
        )
        assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_missing_credential_fails_health_and_jobs_closed(runner, monkeypatch):
    monkeypatch.delenv("TEMPO_VALIDATION_RUNNER_TOKEN")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        assert (await client.get("/healthz")).status_code == 503
        assert (await client.post("/run", json={})).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/run", "/run-stream"])
async def test_authorized_jobs_run_without_runner_credential(runner, path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://test-docker:2375")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        response = await client.post(path, headers={"Authorization": f"Bearer {TOKEN}"}, json={
            "workspace": str(runner),
            "command": 'test -z "$TEMPO_VALIDATION_RUNNER_TOKEN" && printf "%s" "$DOCKER_HOST"',
            "timeout_ms": 1000, "max_output_chars": 1000,
        })
        assert response.status_code == 200
        result = json.loads(response.text.splitlines()[-1])
        assert result["exit_code"] == 0
        assert result["output"] == "tcp://test-docker:2375"
        assert TOKEN not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"timeout_ms": True}, {"timeout_ms": 3_600_001}, {"max_output_chars": "1000"},
    {"environment": {"TOKEN": "no"}}, {"command": ["echo"]}, {"workspace": "/"},
])
async def test_runner_rejects_invalid_authorized_job(runner, change):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_server.application), base_url="http://runner",
    ) as client:
        response = await client.post("/run", headers={"Authorization": f"Bearer {TOKEN}"}, json={
            "workspace": str(runner), "command": "true", "timeout_ms": 1000,
            "max_output_chars": 1000, **change,
        })
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_host_sends_credential_only_in_header_and_suppresses_error_body(runner, monkeypatch):
    async def event(_):
        pass

    validator = ProjectValidator(
        ValidationConfig(runner_url="http://runner"),
        WorkspaceManager(runner.parent, HooksConfig()), event, set(),
    )
    client_class = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in request.content.decode()
        return httpx.Response(403, text=TOKEN)

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_class(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(RuntimeError, match="HTTP 403") as error:
        await validator._run_remote("test", "true", runner, 1000)
    assert TOKEN not in str(error.value)
    assert len(calls) == 1
    monkeypatch.delenv("TEMPO_VALIDATION_RUNNER_TOKEN")
    with pytest.raises(ConfigError):
        await validator._run_remote("test", "true", runner, 1000)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_image", [None, "sha256:" + "b" * 64, "sha256:" + "a" * 64])
async def test_host_requires_result_from_expected_image(runner, monkeypatch, returned_image):
    expected = "sha256:" + "a" * 64

    async def event(_):
        pass

    validator = ProjectValidator(
        ValidationConfig(runner_url="http://runner", runner_image=expected),
        WorkspaceManager(runner.parent, HooksConfig()), event, set(),
    )
    client_class = httpx.AsyncClient

    def handler(request):
        assert json.loads(request.content)["execution_image"] == expected
        result = {"type": "result", "exit_code": 0, "output": "passed"}
        if returned_image is not None:
            result["execution_image"] = returned_image
        return httpx.Response(200, text=json.dumps(result) + "\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    if returned_image == expected:
        assert await validator._run_remote("test", "true", runner, 1000) == (0, "passed")
    else:
        with pytest.raises(RuntimeError, match="different execution image"):
            await validator._run_remote("test", "true", runner, 1000)


def test_credential_provisioning_is_private_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("TEMPO_VALIDATION_RUNNER_TOKEN", raising=False)
    path = Path(__file__).parents[1] / "scripts" / "provision-runner-token.py"
    spec = importlib.util.spec_from_file_location("provision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = tmp_path / ".env"
    env.write_text("EXISTING_SECRET=keep-me\nTEMPO_VALIDATION_RUNNER_TOKEN=\n")
    module.provision(env)
    contents = env.read_text()
    assert "EXISTING_SECRET=keep-me" in contents
    assert env.stat().st_mode & 0o777 == 0o600
    assert len(contents.split("TEMPO_VALIDATION_RUNNER_TOKEN=")[1].strip()) >= 32
    module.provision(env)
    assert env.read_text() == contents
