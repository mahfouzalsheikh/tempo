import base64
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client

from tempo import agent_accounts as accounts
from tempo_web.models import AgentAccount, AgentAccountEvent, Organization, Project

pytestmark = pytest.mark.django_db(transaction=True)


def cache(account="workspace-a", subject="user-a"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": account,
                "id_token": f"header.{claims}.signature",
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
            },
        }
    ).encode()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPO_ACCOUNT_CREDENTIAL_ROOT", str(tmp_path / "credentials"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    user = get_user_model().objects.create_user(username="operator", is_staff=True)
    org = Organization.objects.create(slug="team", name="Team")
    project = Project.objects.create(organization=org, slug="app", name="App")
    source = tmp_path / "auth.json"
    source.write_bytes(cache())
    source.chmod(0o600)
    return user, org, project, source


def test_two_accounts_have_distinct_private_stores_without_secret_metadata(setup):
    user, org, project, source = setup
    one = accounts.create(user, org, "Development", "codex-file")
    two = accounts.create(user, org, "Review", "codex-file")
    one = accounts.provision(user, one.pk, 1, source)
    source.write_bytes(cache(subject="user-b"))
    two = accounts.provision(user, two.pk, 1, source)
    assert accounts.stored_cache(one) == cache()
    assert accounts.stored_cache(two) == cache(subject="user-b")
    assert one.identity_fingerprint != two.identity_fingerprint
    assert len(list(accounts.credential_root().iterdir())) == 2
    for path in accounts.credential_root().iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    client = Client()
    client.force_login(user)
    for url in ["/agents/accounts/", "/api/v1/accounts"]:
        response = client.get(url)
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert (
            b"secret-access" not in response.content and b"secret-refresh" not in response.content
        )
        assert b"header." not in response.content
    metadata = json.dumps(list(AgentAccountEvent.objects.values("detail")))
    assert "secret" not in metadata
    assert accounts.public_record(one)["authentication_verified"] is False
    assert accounts.public_record(one)["runtime_routing"] == "not_available"


def test_grants_are_organization_scoped_revision_checked_and_audited(setup):
    user, org, project, _ = setup
    row = accounts.create(user, org, "Development", "codex-file")
    other = Organization.objects.create(slug="other", name="Other")
    forbidden = Project.objects.create(organization=other, slug="private", name="Private")
    with pytest.raises(ValueError, match="organization"):
        accounts.update(user, row.pk, 1, disabled=False, project_ids=[forbidden.pk])
    row = accounts.update(user, row.pk, 1, disabled=False, project_ids=[project.pk])
    assert row.grants.get().active
    with pytest.raises(ValueError, match="changed"):
        accounts.update(user, row.pk, 1, disabled=True, project_ids=[])
    row = accounts.update(user, row.pk, 2, disabled=True, project_ids=[])
    assert row.disabled and not row.grants.get().active
    assert list(row.events.values_list("action", flat=True)) == [
        "settings_updated",
        "settings_updated",
        "created",
    ]


@pytest.mark.parametrize("kind", ["symlink", "public", "oversized", "directory", "fifo"])
def test_credentials_reject_unsafe_files_before_storage(setup, kind):
    user, org, _, source = setup
    row = accounts.create(user, org, "Development", "codex-file")
    if kind == "symlink":
        link = source.with_name("link.json")
        link.symlink_to(source)
        source = link
    elif kind == "public":
        source.chmod(0o644)
    elif kind == "oversized":
        source.write_bytes(b"a" * (accounts.LIMIT + 1))
    elif kind == "directory":
        source = source.parent
    else:
        source.unlink()
        os.mkfifo(source, 0o600)
    with pytest.raises((ValueError, OSError)):
        accounts.provision(user, row.pk, 1, source)
    row.refresh_from_db()
    assert row.credential_generation == 0
    assert not row.events.filter(action="provisioned").exists()


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"{}", b'{"OPENAI_API_KEY":"secret"}'])
def test_malformed_or_unsupported_auth_never_becomes_configured(setup, payload):
    user, org, _, source = setup
    row = accounts.create(user, org, "Development", "codex-file")
    source.write_bytes(payload)
    with pytest.raises(ValueError, match="supported"):
        accounts.provision(user, row.pk, 1, source)
    row.refresh_from_db()
    assert row.check_status == "pending"


def test_no_overwrite_and_local_identity_change_rejected(setup):
    user, org, _, source = setup
    row = accounts.create(user, org, "Development", "codex-file")
    row = accounts.provision(user, row.pk, 1, source)
    original = row.identity_fingerprint
    with pytest.raises(ValueError):
        accounts.provision(user, row.pk, row.revision, source)
    (accounts.credential_root() / f"{row.pk.hex}.json").write_bytes(cache(subject="different"))
    row = accounts.check(user, row.pk, row.revision)
    assert row.check_status == "unavailable" and row.identity_fingerprint == original


def test_installation_reference_does_not_copy_or_modify_login(setup):
    user, org, _, source = setup
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir()
    auth = home / "auth.json"
    auth.write_bytes(source.read_bytes())
    auth.chmod(0o600)
    row = accounts.create(user, org, "Installation", "installation")
    row = accounts.check(user, row.pk, 1)
    assert row.check_status == "stored" and auth.read_bytes() == cache()
    assert not Path(os.environ["TEMPO_ACCOUNT_CREDENTIAL_ROOT"]).exists()
    auth.unlink()
    row = accounts.check(user, row.pk, row.revision)
    assert row.check_status == "unavailable"


def test_ui_and_api_require_staff_and_csrf_and_reject_secret_input(setup):
    user, org, _, _ = setup
    client = Client()
    assert client.get("/agents/accounts/").status_code == 302
    assert client.get("/api/v1/accounts").status_code == 401
    ordinary = get_user_model().objects.create_user(username="reader")
    client.force_login(ordinary)
    assert client.get("/agents/accounts/").status_code == 403
    assert client.get("/api/v1/accounts").status_code == 403
    with pytest.raises(PermissionDenied):
        accounts.create(ordinary, org, "Forbidden", "codex-file")
    client.force_login(user)
    data = {
        "action": "create",
        "organization_id": org.pk,
        "label": "Development",
        "auth_mode": "codex-file",
    }
    response = client.post("/api/v1/accounts", data, content_type="application/json")
    assert response.status_code == 200
    assert response.json()["account"]["check_status"] == "pending"
    response = client.post(
        "/api/v1/accounts", {**data, "credential": "secret-access"}, content_type="application/json"
    )
    assert response.status_code == 409 and b"secret-access" not in response.content
    assert AgentAccount.objects.count() == 1
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post("/agents/accounts/", {}).status_code == 403
    assert csrf.post("/api/v1/accounts", data, content_type="application/json").status_code == 403
    response = client.post(
        "/agents/accounts/",
        {"action": "create", "organization": org.pk, "label": "Second", "auth_mode": "codex-file"},
    )
    assert response.status_code == 302
    assert b"Agent assignment is not available yet" in client.get("/agents/accounts/").content
    row = AgentAccount.objects.get(label="Development")
    row = accounts.update(user, row.pk, row.revision, disabled=True, project_ids=[])
    accounts.check(user, row.pk, row.revision)
    content = client.get("/agents/accounts/").content
    assert b"Disabled" in content and b"Login unavailable" in content



def test_provision_command_redacts_and_audits(setup):
    user, org, _, source = setup
    row = accounts.create(user, org, "Development", "codex-file")
    output = io.StringIO()
    call_command(
        "provision_codex_account",
        account=str(row.pk),
        revision=1,
        auth_file=str(source),
        operator=user.username,
        stdout=output,
    )
    assert "runtime verification is pending" in output.getvalue()
    assert "secret" not in output.getvalue()
    assert row.events.filter(action="provisioned", actor=user).exists()


def test_credential_volume_is_not_mounted_into_workers_or_sandboxes():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    for name, service in compose["services"].items():
        mounts = [m for m in service.get("volumes", []) if "tempo-account-credentials:" in m]
        assert bool(mounts) == (name == "tempo")


def test_parallel_updates_reject_one_stale_revision(setup):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    user, org, _, _ = setup
    row = accounts.create(user, org, "Development", "codex-file")
    barrier = Barrier(2)

    def update(disabled):
        try:
            barrier.wait(timeout=5)
            try:
                accounts.update(user, row.pk, 1, disabled=disabled, project_ids=[])
                return "updated"
            except ValueError:
                return "stale"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, [True, False])) == ["stale", "updated"]
