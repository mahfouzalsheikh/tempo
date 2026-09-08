"""Disposable validation containers controlled by the authenticated runner."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from pathlib import Path

from .errors import ConfigError
from .process import stop_process_group
from .run_snapshot import execution_setting
from .validation_auth import runner_environment
from .validation_images import allowed_images


def execution_config() -> tuple[str, str | None]:
    backend = execution_setting("TEMPO_VALIDATION_BACKEND", "docker")
    if backend == "process":
        return backend, None
    if backend != "docker":
        raise ConfigError("unsupported validation execution backend")
    image = execution_setting("TEMPO_VALIDATION_IMAGE", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ConfigError("validation requires an immutable Docker image ID")
    allowed_images(image)
    return backend, image


async def select_image(requested):
    backend, default = execution_config()
    if (backend == "process" and requested is not None) or (
        backend == "docker" and (
            not isinstance(requested, str) or requested not in allowed_images(default)
        )
    ):
        raise ConfigError("Image is not approved for this runner.",
                          category="execution_image_mismatch")
    if backend == "docker":
        process = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", "--format", "{{.Id}}", requested,
            env=runner_environment(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except BaseException:
            await stop_process_group(process)
            raise
        if process.returncode or output.decode().strip() != requested:
            raise ConfigError("The exact saved image is not installed locally.",
                              category="execution_image_unavailable")
    return requested


def container_arguments(name: str, image: str, workspace: Path, command: str, timeout_ms: int):
    if "," in str(workspace):
        raise ConfigError("validation workspace paths cannot contain commas")
    return [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--label", "tempo.validation=true", "--network=none", "--read-only", "--init",
        "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=KILL",
        "--security-opt=no-new-privileges", "--user=0:0",
        "--pids-limit=256", "--cpus=2", "--memory=2g", "--memory-swap=2g",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
        # Mask writable VOLUME declarations inherited from the application image.
        "--tmpfs", "/data/workspaces:ro,nosuid,nodev,size=4096",
        "--tmpfs", "/data/database:ro,nosuid,nodev,size=4096",
        "--tmpfs", "/home/tempo/.codex:ro,nosuid,nodev,size=4096",
        "--mount", f"type=bind,src={workspace},dst={workspace}", "--workdir", str(workspace),
        "--env", "HOME=/tmp/tempo-home", "--env", "TMPDIR=/tmp",
        "--env", "CODEX_HOME=/tmp/tempo-home/.codex",
        "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "LANG=C.UTF-8",
        "--entrypoint", "/usr/bin/timeout", image,
        # The watchdog stays root; project code runs as UID 10001 with no effective capabilities.
        # It cannot signal the watchdog or regain privilege through setuid executables.
        "--signal=TERM", "--kill-after=5", f"{timeout_ms / 1000}s",
        "/usr/bin/setpriv", "--reuid=10001", "--regid=10001", "--clear-groups",
        "/bin/bash", "--noprofile", "--norc", "-c", 'mkdir -p "$HOME"; ' + command,
    ]


async def _remove_container_once(name: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "docker", "rm", "--force", name, env=runner_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=10)
    except BaseException:
        await stop_process_group(process)
        raise
    if not process.returncode or b"No such container" in error:
        return True
    if b"removal of container" in error and b"already in progress" in error:
        return False
    raise RuntimeError("execution container cleanup could not be confirmed")


async def remove_container(name: str) -> None:
    async with asyncio.timeout(15):
        while not await _remove_container_once(name):  # noqa: ASYNC110 - poll daemon-owned removal
            await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def command_process(command: str, workspace: Path, timeout_ms: int, *, execution_image=None):
    backend, image = execution_config()
    if execution_image is not None:
        if backend != "docker" or execution_image not in allowed_images(image):
            raise ConfigError("The selected validation image is not approved.")
        image = execution_image
    name = f"tempo-validation-{uuid.uuid4().hex}" if backend == "docker" else None
    arguments = container_arguments(name, image, workspace, command, timeout_ms) if name else [
        "bash", "--noprofile", "--norc", "-c", command,
    ]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments, cwd=workspace, env=runner_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        process.validation_image = image
        yield process
    finally:
        if process and process.returncode is None:
            await stop_process_group(process)
        if name:
            # Never emit a successful final result before cleanup is confirmed.
            await remove_container(name)
