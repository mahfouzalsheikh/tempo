import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
from test_execution_network import docker, execution_daemon  # noqa: F401

from tempo.config import CodexConfig
from tempo.errors import ConfigError
from tempo.workload import (
    execution_scope,
    seed_codex_auth,
    seed_codex_settings,
    seed_runtime_home,
    start_workload,
    state_identity,
)


def test_execution_homes_are_scoped_to_workspace_and_node(tmp_path):
    with execution_scope("run:1:node:first"):
        first = state_identity(tmp_path, "codex")
    with execution_scope("run:1:node:second"):
        assert state_identity(tmp_path, "codex") != first
    with execution_scope("run:1:node:first"):
        assert state_identity(tmp_path, "codex") == first
        assert state_identity(tmp_path / "sibling", "codex") != first
        assert state_identity(tmp_path, "external") != first


def test_legacy_deny_approval_policy_uses_supported_wire_value():
    assert CodexConfig().approval_policy == "never"
    assert CodexConfig(approval_policy={"reject": {
        "sandbox_approval": True, "rules": True, "mcp_elicitations": True,
    }}).approval_policy == "never"


@pytest.mark.parametrize("target", [".codex", ".codex/auth.json"])
def test_auth_seeding_cannot_follow_agent_symlinks(tmp_path, monkeypatch, target):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"token":"test-model-credential"}')
    monkeypatch.setenv("CODEX_HOME", str(source))
    home = tmp_path / "home"
    home.mkdir()
    if target.endswith("auth.json"):
        (home / ".codex").mkdir()
        (home / target).symlink_to(source / "auth.json")
    else:
        (home / target).symlink_to(source, target_is_directory=True)
    with pytest.raises((ConfigError, OSError)):
        seed_codex_auth(home)
    assert json.loads((source / "auth.json").read_text())["token"] == "test-model-credential"


def test_auth_seed_copies_only_login_and_keeps_newer_private_refresh(tmp_path, monkeypatch):
    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir()
    home.mkdir()
    (source / "auth.json").write_text("login-only")
    (source / "config.toml").write_text("other-connector-secret")
    monkeypatch.setenv("CODEX_HOME", str(source))
    seed_codex_auth(home)
    assert (home / ".codex/auth.json").read_text() == "login-only"
    assert not (home / ".codex/config.toml").exists()
    assert (home / ".codex/auth.json").stat().st_mode & 0o777 == 0o600
    (home / ".codex/auth.json").write_text("refreshed-private-login")
    seed_codex_auth(home)
    assert (home / ".codex/auth.json").read_text() == "refreshed-private-login"


def test_portable_model_settings_do_not_import_host_connectors(tmp_path, monkeypatch):
    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir()
    home.mkdir()
    (source / "config.toml").write_text(
        'model = "configured-model"\nmodel_reasoning_effort = "high"\n'
        '[mcp_servers.private]\ncommand = "host-only-command"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source))
    seed_codex_settings(home)
    settings = (home / ".codex/config.toml").read_text()
    assert 'model = "configured-model"' in settings
    assert "host-only-command" not in settings
    assert "mcp_servers" not in settings


@pytest.mark.asyncio
async def test_cancelled_seed_finishes_writes_before_cleanup(tmp_path, monkeypatch):
    from tempo import workload

    started, release = threading.Event(), threading.Event()

    def seed(_):
        started.set()
        assert release.wait(5)
        (tmp_path / "finished").touch()

    monkeypatch.setattr(workload, "seed_codex_settings", seed)
    task = asyncio.create_task(seed_runtime_home(tmp_path, False))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "finished").exists()


@pytest.mark.asyncio
async def test_docker_backend_never_falls_back_when_image_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPO_RUNTIME_BACKEND", "docker")
    monkeypatch.delenv("TEMPO_RUNTIME_IMAGE", raising=False)
    monkeypatch.delenv("TEMPO_VALIDATION_IMAGE", raising=False)
    with pytest.raises(ConfigError, match="immutable"):
        await start_workload("touch unexpected", tmp_path, {}, kind="hook")
    assert not (tmp_path / "unexpected").exists()


def test_runtime_and_hook_isolation_with_durable_session_resume(request):
    _, environment = request.getfixturevalue("execution_daemon")
    root = Path(__file__).resolve().parents[1]
    shared = environment["TEMPO_TEST_SHARED_ROOT"]
    result = docker(
        "run", "--rm", "--network=host", "--entrypoint", "python",
        "--mount", f"type=bind,src={root / 'tempo'},dst=/app/tempo,ro",
        "--mount", f"type=bind,src={root / 'scripts/check-runtime-sandbox.py'},dst=/probe.py,ro",
        "--mount", f"type=bind,src={shared},dst=/runtime-fixtures",
        "--env", "TEMPO_RUNTIME_BACKEND=docker",
        "--env", f"DOCKER_HOST={environment['DOCKER_HOST']}",
        "--env", f"TEMPO_VALIDATION_IMAGE={environment['TEMPO_VALIDATION_IMAGE']}",
        "--env", "TEMPO_WORKSPACE_ROOT=/runtime-fixtures/workspaces",
        "--env", "TEMPO_AGENT_STATE_ROOT=/runtime-fixtures/state",
        os.environ["TEMPO_TEST_VALIDATION_IMAGE"], "/probe.py",
    )
    assert result.returncode == 0, result.stdout + result.stderr
