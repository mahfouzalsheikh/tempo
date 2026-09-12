"""Probe deployed, concurrent account-home isolation with synthetic credentials; no model work."""

import asyncio
import base64
import hashlib
import json
import os
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from tempo.account_binding import SELECTED_ACCOUNT  # noqa: E402
from tempo.agent_accounts import cache_identity  # noqa: E402
from tempo.credentials import process_environment  # noqa: E402
from tempo.workload import execution_scope, start_workload, stop_workload  # noqa: E402

if os.getuid() == 0:
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)


def credential(subject):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": subject,
                "id_token": f"fixture.{claims}.fixture",
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
            },
        }
    ).encode()


async def probe(workspace, subject):
    data = credential(subject)
    binding = {
        "schema": 1,
        "id": str(uuid.uuid4()),
        "project_id": 1,
        "identity": cache_identity(data),
        "generation": 1,
    }
    script = (
        "import hashlib,pathlib; p=pathlib.Path.home()/'.codex/auth.json'; "
        f"assert hashlib.sha256(p.read_bytes()).hexdigest()=={hashlib.sha256(data).hexdigest()!r}; "
        "assert not pathlib.Path('/data/account-credentials').exists(); "
        "assert not pathlib.Path('/run/tempo-host-codex').exists(); "
        "assert not pathlib.Path('/var/run/docker.sock').exists(); print('isolated')"
    )
    token = SELECTED_ACCOUNT.set((binding, data))
    process = None
    try:
        with execution_scope("account-routing-smoke"):
            process = await start_workload(
                "python -c " + shlex.quote(script),
                workspace,
                process_environment(),
                kind="codex",
            )
        output, _ = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0 and output.strip() == b"isolated"
        return str(process.execution_home)
    finally:
        SELECTED_ACCOUNT.reset(token)
        if process:
            await stop_workload(process)
            shutil.rmtree(process.execution_home)


async def main():
    with tempfile.TemporaryDirectory(
        prefix="account-routing-smoke-", dir=os.environ["TEMPO_WORKSPACE_ROOT"]
    ) as directory:
        homes = await asyncio.gather(probe(Path(directory), "one"), probe(Path(directory), "two"))
        assert homes[0] != homes[1]
    print(
        "Concurrent selected-account Docker provisioning passed "
        "(synthetic credentials, no model turns)."
    )


asyncio.run(main())
