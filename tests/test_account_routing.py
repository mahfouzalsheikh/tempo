import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone

from tempo import agent_accounts as accounts
from tempo.account_binding import SELECTED_ACCOUNT
from tempo.account_runtime import AccountBoundRuntime
from tempo.config import ServiceConfig
from tempo.domain import WorkflowDefinition
from tempo.errors import ConfigError
from tempo.run_snapshot import capture_snapshot, restore_snapshot, snapshot_digest
from tempo.workload import execution_scope, seed_codex_auth, seed_codex_settings, state_identity
from tempo_web.models import AgentAccountAttempt, AgentRun, TrackedIssue
from tests.test_agent_accounts import cache
from tests.test_agent_accounts import setup as make_account_setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def account_setup(tmp_path, monkeypatch):
    return make_account_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def routed(account_setup, monkeypatch):
    user, org, project, source = account_setup
    account = accounts.create(user, org, "Worker", "codex-file")
    account = accounts.provision(user, account.pk, account.revision, source)
    account = accounts.update(
        user, account.pk, account.revision, disabled=False, project_ids=[project.pk]
    )
    binding = accounts.assignment(user, account.pk, account.revision, project.pk)
    issue = TrackedIssue.objects.create(
        project=project,
        tracker_kind="memory",
        external_id="1",
        identifier="T-1",
        title="Test",
        state="Todo",
    )
    run = AgentRun.objects.create(project=project, issue=issue, started_at=timezone.now())
    monkeypatch.setenv("TEMPO_RUNTIME_BACKEND", "docker")
    return account, binding, run


class Runtime:
    selected_model = "test-model"

    def __init__(self):
        self.starts = 0
        self.stops = 0

    async def start_session(self, workspace, *, resume_context=None):
        self.starts += 1
        self.selected = SELECTED_ACCOUNT.get()
        await asyncio.sleep(0)
        assert self.selected == SELECTED_ACCOUNT.get()
        return SimpleNamespace(thread_id="thread-" + self.selected[0]["id"])

    async def run_turn(self, *args):
        return {"ok": True}

    async def stop_session(self, session):
        self.stops += 1


def test_snapshot_contract_opt_in_and_identity_pinning(routed, tmp_path):
    _, binding, _ = routed
    config = ServiceConfig.model_validate(
        {
            "tracker": {"kind": "memory", "active_states": ["Todo"], "terminal_states": ["Done"]},
            "workspace": {"root": str(tmp_path)},
            "agents": {"worker": {}},
            "workflow": {"nodes": [{"id": "work", "agent": "worker"}]},
        }
    )
    definition = WorkflowDefinition(
        config={}, prompt_template="prompt", path=tmp_path / "W.md", mtime_ns=0
    )
    legacy = capture_snapshot(definition, config)
    assert legacy["schema"] == 1
    config.agents["worker"].settings["account"] = binding
    snapshot = capture_snapshot(definition, config)
    assert snapshot["schema"] == 2
    _, loaded = restore_snapshot(snapshot, snapshot_digest(snapshot))
    assert loaded.agents["worker"].settings["account"] == binding
    broken = copy.deepcopy(snapshot)
    broken["schema"] = 1
    with pytest.raises(ConfigError):
        restore_snapshot(broken, snapshot_digest(broken))
    assert "secret" not in json.dumps(snapshot)
    for command in ["codex app-server --config foo=bar", "other-runtime"]:
        raw = config.model_dump()
        raw["codex"]["command"] = command
        with pytest.raises(ValueError):
            ServiceConfig.model_validate(raw)


def test_distinct_accounts_concurrently_keep_credentials_and_threads_separate(
    routed, account_setup, tmp_path
):
    one, binding, run = routed
    user, org, project, source = account_setup
    source.write_bytes(cache(subject="second-user"))
    two = accounts.create(user, org, "Reviewer", "codex-file")
    two = accounts.provision(user, two.pk, two.revision, source)
    two = accounts.update(user, two.pk, two.revision, disabled=False, project_ids=[project.pk])
    second = accounts.assignment(user, two.pk, two.revision, project.pk)

    async def task(binding):
        runtime = Runtime()
        wrapped = AccountBoundRuntime(runtime, binding, "worker")
        with execution_scope(f"run:{run.pk}:node:work"):
            session = await wrapped.start_session(tmp_path)
            await wrapped.run_turn(session, "hello", None)
            await wrapped.stop_session(session)
        return runtime.selected

    async def both():
        return await asyncio.gather(task(binding), task(second))

    selected = async_to_sync(both)()
    assert selected[0][1] != selected[1][1]
    assert SELECTED_ACCOUNT.get() is None
    assert set(AgentAccountAttempt.objects.values_list("account_id", flat=True)) == {one.pk, two.pk}
    assert set(AgentAccountAttempt.objects.values_list("status", flat=True)) == {"stopped"}
    assert len(set(AgentAccountAttempt.objects.values_list("thread_id", flat=True))) == 2


@pytest.mark.parametrize(
    "failure", ["disabled", "revoked", "identity", "missing", "wrong_project", "foreign_thread"]
)
def test_account_preflight_blocks_before_runtime_start(routed, tmp_path, failure):
    account, binding, run = routed
    resume = None
    if failure == "disabled":
        account.disabled = True
        account.save()
    elif failure == "revoked":
        account.grants.update(active=False)
    elif failure == "identity":
        (accounts.credential_root() / f"{account.pk.hex}.json").write_bytes(
            cache(subject="intruder")
        )
    elif failure == "missing":
        (accounts.credential_root() / f"{account.pk.hex}.json").unlink()
    elif failure == "wrong_project":
        binding["project_id"] += 1
    else:
        resume = SimpleNamespace(thread_id="foreign-thread")
    runtime = Runtime()
    wrapped = AccountBoundRuntime(runtime, binding, "worker")

    async def start():
        with execution_scope(f"run:{run.pk}:node:work"):
            await wrapped.start_session(tmp_path, resume_context=resume)

    with pytest.raises(ConfigError):
        async_to_sync(start)()
    assert runtime.starts == 0


def test_private_home_never_adopts_other_identity_even_with_newer_mtime(routed, tmp_path):
    account, binding, _ = routed
    home = tmp_path / "home"
    home.mkdir()
    original = state_identity(home, "codex")
    token = SELECTED_ACCOUNT.set((binding, accounts.stored_cache(account)))
    try:
        assert state_identity(home, "codex") != original
        seed_codex_settings(home)
        seed_codex_auth(home)
        path = home / ".codex/auth.json"
        assert path.read_bytes() == cache()
        path.write_bytes(cache(subject="wrong-user"))
        with pytest.raises(ConfigError):
            seed_codex_auth(home)
        assert 'forced_login_method = "chatgpt"' in (home / ".codex/config.toml").read_text()
    finally:
        SELECTED_ACCOUNT.reset(token)


def test_revocation_between_turns_blocks_and_retains_attribution(routed, tmp_path):
    account, binding, run = routed
    runtime = Runtime()
    wrapped = AccountBoundRuntime(runtime, binding, "worker")

    async def execute():
        with execution_scope(f"run:{run.pk}:node:work"):
            session = await wrapped.start_session(tmp_path)
        await account.grants.aupdate(active=False)
        with pytest.raises(ConfigError):
            await wrapped.run_turn(session, "hello", None)
        await wrapped.stop_session(session)

    async_to_sync(execute)()
    assert AgentAccountAttempt.objects.get().status == "blocked"
    assert runtime.stops == 1


def test_assignment_api_pins_new_runs_and_rejects_stale_forms(account_setup, tmp_path):
    import yaml
    from django.test import Client

    from tempo.domain import Issue
    from tempo.orchestrator import Orchestrator
    from tempo.runtime import set_orchestrator
    from tests.test_run_snapshot import configuration

    user, org, _, source = account_setup
    config = configuration(tmp_path)
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\n" + yaml.safe_dump(config.model_dump(mode="json")) + "---\nprompt")
    controller = Orchestrator(str(path))

    async def initialize():
        await controller.store.initialize()
        await controller._apply_config(controller.store.current()[1])

    async_to_sync(initialize)()
    project_id = controller.persistence.project_id
    from tempo_web.models import Project

    project = Project.objects.get(pk=project_id)
    account = accounts.create(user, project.organization, "Assigned", "codex-file")
    account = accounts.provision(user, account.pk, account.revision, source)
    account = accounts.update(
        user, account.pk, account.revision, disabled=False, project_ids=[project_id]
    )
    original_digest = snapshot_digest(controller.persistence.execution_snapshot)
    old_id = async_to_sync(controller.persistence.enqueue_issue)(
        Issue(id="old", identifier="T-OLD", title="Old", state="Todo")
    )
    set_orchestrator(controller)
    try:
        client = Client()
        client.force_login(user)
        payload = {
            "action": "assign",
            "project_id": project_id,
            "profile": "worker",
            "account_id": str(account.pk),
            "revision": account.revision,
            "configuration_digest": original_digest,
        }
        assert (
            client.post("/api/v1/accounts", payload, content_type="application/json").status_code
            == 200
        )
        assert (
            client.post("/api/v1/accounts", payload, content_type="application/json").status_code
            == 409
        )
        assert b"Assigned" in client.get("/agents/accounts/").content
        new_id = async_to_sync(controller.persistence.enqueue_issue)(
            Issue(id="new", identifier="T-NEW", title="New", state="Todo")
        )
        assert AgentRun.objects.get(pk=old_id).snapshot_digest == original_digest
        new = AgentRun.objects.get(pk=new_id)
        assert new.execution_snapshot["schema"] == 2
        assert new.execution_snapshot["config"]["agents"]["worker"]["settings"]["account"][
            "id"
        ] == str(account.pk)
        payload.update(
            account_id="",
            revision=0,
            configuration_digest=snapshot_digest(controller.persistence.execution_snapshot),
        )
        assert (
            client.post("/api/v1/accounts", payload, content_type="application/json").status_code
            == 200
        )
        assert "account" not in controller.persistence.config.agents["worker"].settings
        assert AgentRun.objects.get(pk=new_id).execution_snapshot == new.execution_snapshot
    finally:
        set_orchestrator(None)


def test_exact_account_thread_can_resume_and_failed_start_resets_context(routed, tmp_path):
    _, binding, run = routed

    async def execute():
        runtime = Runtime()
        first = AccountBoundRuntime(runtime, binding, "worker")
        with execution_scope(f"run:{run.pk}:node:work"):
            session = await first.start_session(tmp_path)
            await first.stop_session(session)
            second = AccountBoundRuntime(runtime, binding, "worker")
            resumed = await second.start_session(tmp_path, resume_context=session)
            await second.stop_session(resumed)

            class Broken(Runtime):
                async def start_session(self, *args, **kwargs):
                    raise RuntimeError("provider unavailable")

            with pytest.raises(RuntimeError):
                await AccountBoundRuntime(Broken(), binding, "worker").start_session(tmp_path)
        assert SELECTED_ACCOUNT.get() is None

    async_to_sync(execute)()
    assert list(AgentAccountAttempt.objects.values_list("status", flat=True)) == [
        "failed",
        "stopped",
        "stopped",
    ]


def test_concurrent_private_home_seeding_uses_only_selected_credentials(
    routed, tmp_path, monkeypatch
):
    from tempo.workload import prepare_home, seed_runtime_home

    account, binding, _ = routed
    monkeypatch.setenv("TEMPO_AGENT_STATE_ROOT", str(tmp_path / "homes"))
    # Distinct synthetic identities exercise actual threaded provisioning concurrently.
    second = {
        **binding,
        "identity": accounts.cache_identity(cache(subject="second")),
        "id": "11111111-1111-4111-8111-111111111111",
    }

    async def seed(reference, data):
        token = SELECTED_ACCOUNT.set((reference, data))
        try:
            home = prepare_home(state_identity(tmp_path, "codex"))
            await seed_runtime_home(home, True)
            assert (home / ".codex/auth.json").read_bytes() == data
            return home
        finally:
            SELECTED_ACCOUNT.reset(token)

    one_data = accounts.stored_cache(account)

    async def both():
        return await asyncio.gather(seed(binding, one_data), seed(second, cache(subject="second")))

    homes = async_to_sync(both)()
    assert homes[0] != homes[1]


def test_cleanup_failure_is_not_reported_as_stopped(routed, tmp_path):
    _, binding, run = routed

    class BrokenCleanup(Runtime):
        async def stop_session(self, session):
            raise RuntimeError("container cleanup failed")

    async def execute():
        runtime = AccountBoundRuntime(BrokenCleanup(), binding, "worker")
        with execution_scope(f"run:{run.pk}:node:work"):
            session = await runtime.start_session(tmp_path)
        with pytest.raises(RuntimeError):
            await runtime.stop_session(session)

    async_to_sync(execute)()
    assert AgentAccountAttempt.objects.get().status == "cleanup_failed"
