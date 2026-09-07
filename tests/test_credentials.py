import importlib
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection

from tempo.agent_runtime import CodexAgentRuntime, ExternalCommandRuntime, ModelSelection
from tempo.config import HooksConfig, RuntimeProviderConfig, ValidationConfig, build_config
from tempo.credentials import CONTROL_PLANE_SECRETS, process_environment
from tempo.errors import ConfigError, WorkspaceError
from tempo.persistence import PersistenceStore
from tempo.trackers.github import build_tracker
from tempo.trackers.memory import MemoryTracker
from tempo.validation import ProjectValidator
from tempo.validation_server import run_command, stream_command
from tempo.workspace import WorkspaceManager
from tempo_web.models import WorkflowVersion


def config_payload(tmp_path):
    return {
        "tracker": {
            "kind": "github", "provider": {"repo": "owner/repo", "token": "$PRIVATE_GH"},
            "active_states": ["open"], "terminal_states": ["closed"],
        },
        "workspace": {"root": str(tmp_path)},
    }


def test_config_preserves_references_and_resolves_only_at_tracker_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIVATE_GH", raising=False)
    config = build_config(config_payload(tmp_path), tmp_path / "WORKFLOW.md")
    assert config.tracker.provider["token"] == "$PRIVATE_GH"
    with pytest.raises(ConfigError, match="PRIVATE_GH is not set"):
        build_tracker("github", config.tracker.provider, ["closed"])


@pytest.mark.asyncio
async def test_custom_tracker_reference_resolves_late_and_remains_forbidden(tmp_path, monkeypatch):
    config = build_config(config_payload(tmp_path), tmp_path / "WORKFLOW.md")
    monkeypatch.setenv("PRIVATE_GH", "sentinel-publication-credential")
    monkeypatch.delenv("GITHUB_REVIEW_TOKEN", raising=False)
    tracker = build_tracker("github", config.tracker.provider, ["closed"])
    try:
        assert tracker.token == "sentinel-publication-credential"
        assert "PRIVATE_GH" in tracker.secret_environment_names()
        assert "sentinel-publication-credential" not in config.model_dump_json()
        with pytest.raises(ConfigError, match="control-plane credentials"):
            process_environment(
                {"MODEL_KEY": "$PRIVATE_GH"}, forbidden=tracker.secret_environment_names(),
            )
    finally:
        await tracker.close()


@pytest.mark.parametrize("key", ["token", "api_key", "review_token"])
def test_inline_tracker_credentials_rejected_without_error_disclosure(tmp_path, key):
    payload = config_payload(tmp_path)
    payload["tracker"]["provider"][key] = "never-print-this-secret"
    with pytest.raises(ConfigError) as error:
        build_config(payload, tmp_path / "WORKFLOW.md")
    assert "never-print-this-secret" not in str(error.value)
    assert "$ENVIRONMENT_VARIABLE reference" in str(error.value)


@pytest.mark.parametrize("reference", ["literal-secret", "$BAD-NAME", "$1INVALID", "${BRACES}"])
def test_subprocess_grants_require_references(reference):
    with pytest.raises(ValueError) as error:
        RuntimeProviderConfig(environment={"MODEL_KEY": reference})
    assert reference not in str(error.value)


@pytest.mark.parametrize("target,source", [
    ("MODEL_KEY", "$DATABASE_URL"), ("GITHUB_TOKEN", "$MODEL_SOURCE"),
    ("MODEL_KEY", "$TEMPO_POSTGRES_PASSWORD"),
    ("BASH_ENV", "$MODEL_SOURCE"), ("LD_PRELOAD", "$MODEL_SOURCE"),
])
def test_grants_cannot_alias_control_plane_credentials_or_inject_shell_startup(target, source):
    with pytest.raises(ConfigError):
        process_environment({target: source}, forbidden=CONTROL_PLANE_SECRETS)


@pytest.fixture
def poisoned_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    startup = home / ".bash_profile"
    startup.write_text("export PROFILE_SECRET=profile-leak\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BASH_ENV", str(startup))
    for key in [
        "UNEXPECTED_SECRET", "DATABASE_URL", "GITHUB_TOKEN", "PRIVATE_GH", "OPENAI_API_KEY",
    ]:
        monkeypatch.setenv(key, "ambient-credential-sentinel")
    monkeypatch.setenv("MODEL_SOURCE", "explicit-model-sentinel")


def assert_environment_scoped(environment):
    assert "PATH" in environment
    for key in [
        "UNEXPECTED_SECRET", "DATABASE_URL", "GITHUB_TOKEN", "PRIVATE_GH",
        "OPENAI_API_KEY", "BASH_ENV", "PROFILE_SECRET", "MODEL_SOURCE",
    ]:
        assert key not in environment


async def ignore_event(event):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["codex", "external"])
async def test_real_runtime_receives_only_its_grant(tmp_path, poisoned_environment, kind):
    output = tmp_path / "environment.json"
    fixture = Path(__file__).parent / "fixtures" / (
        "fake_app_server.py" if kind == "codex" else "fake_external_runtime.py"
    )
    wrapper = tmp_path / "runtime.py"
    wrapper.write_text(
        "import json, os, pathlib, runpy\n"
        f"pathlib.Path({str(output)!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"runpy.run_path({str(fixture)!r}, run_name='__main__')\n"
    )
    runtime_config = RuntimeProviderConfig(
        kind=kind, command=shlex.join([sys.executable, str(wrapper)]),
        environment={"MODEL_KEY": "$MODEL_SOURCE"},
    )
    config = build_config(config_payload(tmp_path), tmp_path / "WORKFLOW.md")
    manager = WorkspaceManager(tmp_path / "root", HooksConfig())
    workspace = await manager.create("test")
    runtime_class = CodexAgentRuntime if kind == "codex" else ExternalCommandRuntime
    runtime = runtime_class(
        config, runtime_config, ModelSelection("fake", None, (), {}), None,
        manager, MemoryTracker(), ignore_event, None,
    )
    session = await runtime.start_session(workspace.path)
    try:
        environment = json.loads(output.read_text())
        assert_environment_scoped(environment)
        assert environment["MODEL_KEY"] == "explicit-model-sentinel"
    finally:
        await runtime.stop_session(session)


@pytest.mark.asyncio
async def test_hooks_grant_clone_token_and_redact_failures(tmp_path, poisoned_environment):
    manager = WorkspaceManager(tmp_path / "root", HooksConfig(
        environment={"CLONE_KEY": "$PRIVATE_GH"},
    ))
    workspace = await manager.create("hook")
    script = shlex.join([sys.executable, "-c", (
        "import json, os, pathlib; pathlib.Path('environment.json').write_text("
        "json.dumps(dict(os.environ)))"
    )])
    await manager.run_hook("before_run", script, workspace.path, fatal=True)
    environment = json.loads((workspace.path / "environment.json").read_text())
    assert_environment_scoped(environment)
    assert environment["CLONE_KEY"] == "ambient-credential-sentinel"
    with pytest.raises(WorkspaceError) as error:
        await manager.run_hook(
            "before_run", 'echo "$CLONE_KEY"; exit 1', workspace.path, fatal=True,
        )
    assert "ambient-credential-sentinel" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["local", "remote", "stream"])
async def test_validation_processes_do_not_inherit_host_credentials(
    tmp_path, poisoned_environment, mode,
):
    command = shlex.join([
        sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))",
    ])
    if mode == "local":
        validator = ProjectValidator(
            ValidationConfig(), WorkspaceManager(tmp_path, HooksConfig()), ignore_event, set(),
        )
        exit_code, output = await validator._run_local(command, tmp_path, 1000)
        result = {"exit_code": exit_code, "output": output}
    elif mode == "remote":
        result = await run_command(command, tmp_path, 1000, 10000)
    else:
        events = [event async for event in stream_command(command, tmp_path, 1000, 10000)]
        result = events[-1]
    assert result["exit_code"] == 0
    assert_environment_scoped(json.loads(result["output"]))


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_saved_workflow_contains_references_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_GH", "sentinel-never-persist")
    payload = config_payload(tmp_path)
    payload["runtime_providers"] = {"codex": {"environment": {"MODEL_KEY": "$MODEL_SOURCE"}}}
    config = build_config(payload, tmp_path / "WORKFLOW.md")
    store = PersistenceStore("github", config=config)
    await store.initialize()
    row = await WorkflowVersion.objects.aget(pk=store.workflow_version_id)
    assert row.config["tracker"]["provider"]["token"] == "$PRIVATE_GH"
    assert "sentinel-never-persist" not in json.dumps(row.config)
    sections, _ = await store.workflow_configuration()
    assert sections["runtime_providers"]["codex"]["environment"] == {"MODEL_KEY": "$MODEL_SOURCE"}


@pytest.mark.django_db
def test_historical_snapshot_migration_redacts_literals_without_changing_identity():
    from tempo_web.models import Organization, Project

    organization = Organization.objects.create(slug="redaction", name="Redaction")
    project = Project.objects.create(organization=organization, slug="redaction", name="Redaction")
    row = WorkflowVersion.objects.create(
        project=project, version=1, checksum="a" * 64, path="/old/workflow.md",
        config={"tracker": {"provider": {
            "repo": "owner/repo", "token": "legacy-literal", "review_token": "$REVIEW_TOKEN",
            "api_key": {"unexpected": "legacy-secret"},
        }}, "codex": {"command": "codex app-server"}},
    )
    migration = importlib.import_module("tempo_web.migrations.0011_redact_workflow_credentials")
    for _ in range(2):
        migration.redact_workflow_credentials(apps, SimpleNamespace(connection=connection))
    row.refresh_from_db()
    assert row.config["tracker"]["provider"] == {
        "repo": "owner/repo", "token": "[REDACTED]", "review_token": "$REVIEW_TOKEN",
        "api_key": "[REDACTED]",
    }
    assert row.checksum == "a" * 64
    assert row.config["codex"]["command"] == "codex app-server"
