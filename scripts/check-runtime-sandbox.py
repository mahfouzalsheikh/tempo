"""Exercise deployed runtime boundaries without starting a model turn or changing issues."""

import asyncio
import contextlib
import os
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path

from tempo.agent_runtime import ExternalCommandRuntime, ModelSelection, RuntimeResumeContext
from tempo.codex import CodexAppServer
from tempo.config import HooksConfig, RuntimeProviderConfig, build_config
from tempo.errors import ConfigError, WorkspaceError
from tempo.trackers.memory import MemoryTracker
from tempo.workload import docker_control, execution_scope, start_workload, stop_workload
from tempo.workspace import WorkspaceManager

if os.getuid() == 0:
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)

FAKE = '''
import json, os, pathlib, sys
history = pathlib.Path.home() / 'private-history'
for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get('method'), request.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'userAgent': 'isolation-fixture'}
    elif method in ('thread/start', 'thread/resume', 'session/start'):
        resume = method == 'thread/resume' or bool(params.get('resume'))
        assert not resume or history.exists(), 'private history was lost'
        assert os.getuid() == 10001
        assert not os.getenv('DOCKER_HOST')
        assert not os.getenv('GITHUB_TOKEN')
        assert not os.getenv('TEMPO_VALIDATION_RUNNER_TOKEN')
        assert not pathlib.Path('/run/tempo-host-codex').exists()
        assert not pathlib.Path('/var/run/docker.sock').exists()
        assert not pathlib.Path('/home/tempo/.codex/auth.json').exists()
        history.write_text('persisted')
        result = {'thread': {'id': 'fixture-thread'}, 'resumed': resume}
    if 'id' in request:
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
'''


async def event(_):
    pass


async def main():
    root = Path(os.environ["TEMPO_WORKSPACE_ROOT"])
    os.environ["TEMPO_SMOKE_MODEL_KEY"] = "explicit-model-grant-sentinel"
    homes = set()
    with tempfile.TemporaryDirectory(prefix="runtime-smoke-", dir=root) as directory:
        workspace = Path(directory)
        (workspace / "fake.py").write_text(FAKE)
        sibling = workspace.parent / f"runtime-sibling-{uuid.uuid4().hex}"
        sibling.write_text("other-workspace")
        manager = WorkspaceManager(root, HooksConfig())
        config = build_config({
            "tracker": {"kind": "memory", "active_states": ["open"],
                        "terminal_states": ["closed"]}, "workspace": {"root": str(root)},
            "validation": {"enabled": False},
            "codex": {"command": "python fake.py", "read_timeout_ms": 10000,
                      "environment": {"OPENAI_API_KEY": "$TEMPO_SMOKE_MODEL_KEY"}},
        }, workspace / "WORKFLOW.md")
        try:
            for kind in ("codex", "external"):
                client = (CodexAppServer(config, manager, MemoryTracker(), event) if kind == "codex"
                          else ExternalCommandRuntime(
                              config,
                              RuntimeProviderConfig(kind="external", command="python fake.py",
                                  environment={"OPENAI_API_KEY": "$TEMPO_SMOKE_MODEL_KEY"}),
                              ModelSelection("fake", None, (), {}), None,
                              manager, MemoryTracker(), event, None,
                          ))
                with execution_scope(f"smoke:{workspace.name}:{kind}"):
                    session = await client.start_session(workspace)
                    home = session.process.execution_home
                    homes.add(home)
                    try:
                        assert (home / "private-history").read_text() == "persisted"
                        # A second launch cannot steal or remove the active node's container.
                        with contextlib.suppress(ConfigError):
                            unexpected = await client.start_session(workspace)
                            await client.stop_session(unexpected)
                            raise AssertionError("concurrent session reused the same private home")
                        await docker_control("inspect", session.process.execution_container)
                        with execution_scope(f"smoke:{workspace.name}:{kind}:sibling"):
                            separate = await start_workload(
                                'test ! -e "$HOME/private-history" && '
                                f"test ! -e {shlex.quote(str(home / 'private-history'))} && "
                                "echo separate; sleep 30", workspace, {}, kind="external",
                            )
                        homes.add(separate.execution_home)
                        try:
                            assert await asyncio.wait_for(
                                separate.stdout.readline(), 10,
                            ) == b"separate\n"
                            assert separate.execution_home != home
                        finally:
                            await stop_workload(separate)
                    finally:
                        await client.stop_session(session)
                    previous = session
                    resume = ({"resume_thread_id": "fixture-thread"} if kind == "codex" else {
                        "resume_context": RuntimeResumeContext("fixture-thread", {}),
                    })
                    session = await client.start_session(workspace, **resume)
                    try:
                        assert session.resumed
                        await client.stop_session(previous)
                        await docker_control("inspect", session.process.execution_container)
                        assert session.process.execution_home == home
                    finally:
                        await client.stop_session(session)

            os.environ["TEMPO_SMOKE_HOOK_KEY"] = "explicit-hook-grant-sentinel"
            hook_manager = WorkspaceManager(root, HooksConfig(
                environment={"CLONE_KEY": "$TEMPO_SMOKE_HOOK_KEY"}, timeout_ms=10000,
            ))
            probe = f"""
import os, pathlib
assert os.getuid() == 10001
assert os.environ['CLONE_KEY'] == 'explicit-hook-grant-sentinel'
assert not os.getenv('GITHUB_TOKEN')
assert not os.getenv('DOCKER_HOST')
assert not os.getenv('TEMPO_VALIDATION_RUNNER_TOKEN')
assert not pathlib.Path({str(sibling)!r}).exists()
assert not pathlib.Path('/data/agent-state').exists()
assert not pathlib.Path('/run/tempo-host-codex').exists()
assert not pathlib.Path('/home/tempo/.codex/auth.json').exists()
assert not pathlib.Path(os.environ['CODEX_HOME'], 'auth.json').exists()
pathlib.Path('hook-result').write_text('ok')
"""
            await hook_manager.run_hook("before_run", shlex.join(["python", "-c", probe]),
                                        workspace, fatal=True)
            try:
                await hook_manager.run_hook("before_run", 'echo "$CLONE_KEY"; exit 7',
                                            workspace, fatal=True)
            except WorkspaceError as error:
                assert "explicit-hook-grant-sentinel" not in str(error)
                assert "[REDACTED]" in str(error)
            else:
                raise AssertionError("failed hook was accepted")
            process = await start_workload(
                "echo ready; (sleep 2; touch escaped) & sleep 30", workspace, {},
                kind="hook", timeout_ms=30000,
            )
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
            await stop_workload(process)
            await asyncio.sleep(2)
            assert not (workspace / "escaped").exists()
            process = await start_workload("sleep 30", workspace, {}, kind="hook", timeout_ms=150)
            try:
                await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode in {124, 137}
            finally:
                await stop_workload(process)

            if os.getenv("TEMPO_SMOKE_REAL_CODEX") == "1":
                model_environment = ({"OPENAI_API_KEY": "$OPENAI_API_KEY"}
                                     if os.getenv("OPENAI_API_KEY") else {})
                real_config = config.model_copy(update={"codex": config.codex.model_copy(update={
                    "command": "codex app-server", "read_timeout_ms": 30000,
                    "environment": model_environment,
                })})
                client = CodexAppServer(real_config, manager, MemoryTracker(), event)
                with execution_scope(f"smoke:{workspace.name}:real"):
                    session = await client.start_session(workspace)
                homes.add(session.process.execution_home)
                try:
                    request_id = session.next_request_id
                    await client._send(session, {"id": request_id, "method": "account/read",
                                                 "params": {"refreshToken": False}})
                    account = await client._response(session, request_id, 30000)
                    assert account.get("account"), "Codex did not recognize the seeded model login"
                finally:
                    await client.stop_session(session)
                print("Real Codex initialization and login recognition passed; no model turn run.")
        finally:
            sibling.unlink(missing_ok=True)
            for home in homes:
                shutil.rmtree(home)
    print("Runtime resume, exclusive homes, hook grants, redaction, and cleanup passed.")


asyncio.run(main())
