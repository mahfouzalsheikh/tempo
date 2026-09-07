"""Container lifecycle for coding runtimes and explicitly granted lifecycle hooks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import stat
import uuid
from contextvars import ContextVar
from pathlib import Path

from .credentials import BASE_ENVIRONMENT
from .errors import ConfigError
from .process import stop_process_group
from .run_snapshot import execution_setting, portable_model_settings
from .validation_auth import runner_environment
from .validation_sandbox import container_arguments, remove_container

EXECUTION_SCOPE = ContextVar("execution_scope", default="standalone")
EXECUTION_SECRETS = frozenset({
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "TEMPO_VALIDATION_RUNNER_TOKEN",
})


@contextlib.contextmanager
def execution_scope(identity: str):
    token = EXECUTION_SCOPE.set(identity)
    try:
        yield
    finally:
        EXECUTION_SCOPE.reset(token)


def execution_backend() -> str:
    backend = execution_setting("TEMPO_RUNTIME_BACKEND", "process")
    if backend not in {"docker", "process"}:
        raise ConfigError("unsupported runtime execution backend")
    return backend


def state_identity(workspace: Path, kind: str) -> str:
    return hashlib.sha256(
        json.dumps([str(workspace.resolve()), EXECUTION_SCOPE.get(), kind]).encode(),
    ).hexdigest()


def prepare_home(identity: str) -> Path:
    root = Path(execution_setting("TEMPO_AGENT_STATE_ROOT", "/data/agent-state"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = root / identity
    home.mkdir(mode=0o700, exist_ok=True)
    # The mount root cannot be replaced by a process inside that mount.
    if home.is_symlink():
        raise ConfigError("runtime home cannot be a symlink")
    return home


def seed_codex_auth(home: Path) -> None:
    """Copy only the model login, with no traversal through agent-controlled symlinks."""
    source = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if not source.exists():
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        home_fd = os.open(home, flags)
        stack.callback(os.close, home_fd)
        try:
            os.mkdir(".codex", mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        codex_fd = os.open(".codex", flags, dir_fd=home_fd)
        stack.callback(os.close, codex_fd)
        try:
            current = os.stat("auth.json", dir_fd=codex_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise ConfigError("runtime auth cache must be a regular file")
            if current.st_mtime_ns >= source.stat().st_mtime_ns:
                return  # Keep credentials refreshed within this node's private home.
        except FileNotFoundError:
            pass
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(source_fd, "rb") as stream:
            credential = stream.read(1024 * 1024 + 1)
        if len(credential) > 1024 * 1024:
            raise ConfigError("model auth cache exceeds the supported size")
        temporary = f".auth-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=codex_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(credential)
            os.replace(temporary, "auth.json", src_dir_fd=codex_fd, dst_dir_fd=codex_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=codex_fd)


def seed_codex_settings(home: Path) -> None:
    portable = portable_model_settings()
    portable["cli_auth_credentials_store"] = "file"
    content = "\n".join(f"{key} = {json.dumps(value)}" for key, value in portable.items()) + "\n"
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        home_fd = os.open(home, flags)
        stack.callback(os.close, home_fd)
        try:
            os.mkdir(".codex", mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        codex_fd = os.open(".codex", flags, dir_fd=home_fd)
        stack.callback(os.close, codex_fd)
        temporary = f".config-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=codex_fd)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            os.replace(temporary, "config.toml", src_dir_fd=codex_fd, dst_dir_fd=codex_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=codex_fd)


async def docker_control(*arguments: str, environment=None) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *arguments, env=environment or runner_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 20)
    except BaseException:
        await stop_process_group(process)
        raise
    if process.returncode:
        # Docker errors may include configured environment values; keep them out of run history.
        if arguments[0] == "create" and b"Conflict." in error:
            raise ConfigError(
                "runtime home is still owned by an earlier session; inspect its cleanup",
            )
        raise ConfigError(f"runtime container {arguments[0]} failed")
    return output.decode().strip()


async def seed_runtime_home(home: Path, include_auth: bool) -> None:
    def seed():
        seed_codex_settings(home)
        if include_auth:
            seed_codex_auth(home)

    task = asyncio.create_task(asyncio.to_thread(seed))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # A thread cannot be cancelled. Keep the container reservation until writes finish.
        await task
        raise


async def start_workload(
    command: str, workspace: Path, environment: dict[str, str], *, kind: str,
    timeout_ms: int | None = None, limit: int = 65536,
    stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
) -> asyncio.subprocess.Process:
    if execution_backend() == "process":
        return await asyncio.create_subprocess_exec(
            "bash", "--noprofile", "--norc", "-c", command, cwd=workspace, env=environment,
            stdin=stdin, stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=limit,
            start_new_session=True,
        )
    image = (execution_setting("TEMPO_RUNTIME_IMAGE")
             or execution_setting("TEMPO_VALIDATION_IMAGE", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ConfigError("runtime execution requires an immutable image ID")
    network = json.loads(await docker_control("network", "inspect", "tempo-agents"))[0]
    if (network["Driver"] != "bridge" or network["EnableIPv6"]
        or network["Options"].get("com.docker.network.bridge.name") != "tempo-agents0"
        or network["Options"].get("com.docker.network.bridge.enable_icc") != "false"
        or network["Labels"].get("tempo.execution-policy") != "public-web-v1"):
        raise ConfigError("runtime execution network does not match policy")
    duration = timeout_ms or int(execution_setting("TEMPO_RUNTIME_TIMEOUT_MS", "28800000"))
    if not 0 < duration <= 86_400_000:
        raise ConfigError("runtime lifetime must be between one millisecond and 24 hours")
    persistent = kind in {"codex", "external"}
    identity = state_identity(workspace, kind) if persistent else uuid.uuid4().hex
    name = f"tempo-runtime-{identity}"
    home = await asyncio.to_thread(prepare_home, identity) if persistent else None
    if home and "," in str(home):
        raise ConfigError("runtime home paths cannot contain commas")
    arguments = container_arguments(name, image, workspace, command, duration)
    arguments[1] = "create"
    arguments[arguments.index("--network=none")] = "--network=tempo-agents"
    arguments[arguments.index("tempo.validation=true")] = "tempo.runtime=true"
    arguments.insert(2, "--interactive")
    index = arguments.index("--entrypoint")
    extra = {key: value for key, value in environment.items() if key not in BASE_ENVIRONMENT}
    if any(key in EXECUTION_SECRETS or key.startswith("DOCKER_") or key == "CODEX_HOME"
           for key in extra):
        raise ConfigError("runtime environment cannot override execution controls")
    extra_arguments = []
    for key in extra:
        extra_arguments.extend(["--env", key])  # Values stay out of argv.
    if home:
        extra_arguments.extend(["--mount", f"type=bind,src={home},dst=/home/agent",
                                "--env", "HOME=/home/agent",
                                "--env", "CODEX_HOME=/home/agent/.codex"])
    arguments[index:index] = extra_arguments
    arguments.insert(arguments.index("--signal=TERM"), "--foreground")
    # Reserve a stable name before touching persistent auth: an orphaned session fails closed.
    # A failed create must never remove a container belonging to an earlier session.
    launch = uuid.uuid4().hex
    arguments[2:2] = ["--label", f"tempo.launch={launch}"]
    try:
        container_id = await docker_control(
            *arguments[1:], environment={**runner_environment(), **extra},
        )
    except BaseException:
        # Reconcile a lost create response, but never remove another launch's reservation.
        try:
            detail = json.loads(await docker_control("inspect", name))[0]
        except ConfigError:
            pass
        else:
            if detail["Config"]["Labels"].get("tempo.launch") == launch:
                await remove_container(detail["Id"])
        raise
    process = None
    try:
        if kind == "codex":
            await seed_runtime_home(home, "OPENAI_API_KEY" not in extra)
        process = await asyncio.create_subprocess_exec(
            "docker", "start", "--attach", "--interactive", container_id, env=runner_environment(),
            stdin=stdin, stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=limit,
            start_new_session=True,
        )
        process.execution_container = container_id
        process.execution_image = image
        process.execution_home = home
        return process
    except BaseException:
        if process:
            await stop_process_group(process)
        await remove_container(container_id)
        raise


async def stop_workload(process: asyncio.subprocess.Process) -> None:
    try:
        await stop_process_group(process)
    finally:
        if name := getattr(process, "execution_container", None):
            await remove_container(name)
