"""Supervise one offline browser container with read-only inputs and bounded output."""

import asyncio
import json
import re

from tempo.process import stop_process_group
from tempo.validation_auth import runner_environment
from tempo.validation_sandbox import remove_container

TIMEOUT = 120


class CleanupError(RuntimeError):
    pass


def arguments(image, directory, name):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image) or "," in str(directory):
        raise ValueError("Acceptance requires an immutable image and safe input directory")
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--label",
        "tempo.acceptance=true",
        "--network=none",
        "--read-only",
        "--init",
        "--cap-drop=ALL",
        "--cap-add=SETUID",
        "--cap-add=SETGID",
        "--cap-add=KILL",
        "--security-opt=no-new-privileges",
        "--user=0:0",
        "--pids-limit=256",
        "--cpus=2",
        "--memory=2g",
        "--memory-swap=2g",
        "--shm-size=256m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
        "--mount",
        f"type=bind,src={directory},dst=/input,readonly",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--entrypoint",
        "/usr/bin/timeout",
        image,
        "--signal=TERM",
        "--kill-after=5",
        f"{TIMEOUT}s",
        "/usr/bin/setpriv",
        "--reuid=10001",
        "--regid=10001",
        "--clear-groups",
        "python",
        "-m",
        "tempo.acceptance_harness",
    ]


async def bounded(stream):
    data = bytearray()
    while chunk := await stream.read(65536):
        data.extend(chunk)
        if len(data) > 1_000_000:
            raise ValueError("Browser runner output exceeded its limit")
    return bytes(data)


async def execute(image, directory, key):
    name = f"tempo-acceptance-{key.hex}"
    process = None
    readers = []
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments(image, directory, name),
            env=runner_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        readers = [
            asyncio.create_task(bounded(process.stdout)),
            asyncio.create_task(bounded(process.stderr)),
        ]
        async with asyncio.timeout(TIMEOUT + 20):
            output, error = await asyncio.gather(*readers)
            await process.wait()
        if process.returncode:
            raise ValueError(
                "Browser runner failed or timed out: " + error.decode(errors="replace")[-2000:]
            )
        return json.loads(output)
    finally:
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if process and process.returncode is None:
            await stop_process_group(process)
        try:
            await remove_container(name)
        except Exception as error:
            raise CleanupError("Browser container cleanup is pending.") from error
