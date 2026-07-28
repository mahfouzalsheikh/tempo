from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
from typing import Any

import uvicorn

ROOT = Path(os.getenv("TEMPO_WORKSPACE_ROOT", "/data/workspaces")).resolve(strict=False)
MAX_REQUEST_BYTES = 1024 * 1024


async def application(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    path = scope.get("path", "")
    method = scope.get("method", "GET")
    if method == "GET" and path == "/healthz":
        await _respond(send, 200, {"status": "ok"})
        return
    if method != "POST" or path not in {"/run", "/run-stream"}:
        await _respond(send, 404, {"error": "not_found"})
        return
    body = bytearray()
    while True:
        message = await receive()
        body.extend(message.get("body", b""))
        if len(body) > MAX_REQUEST_BYTES:
            await _respond(send, 413, {"error": "request_too_large"})
            return
        if not message.get("more_body"):
            break
    try:
        payload = json.loads(body)
        workspace = Path(str(payload["workspace"])).resolve(  # noqa: ASYNC240
            strict=True
        )
        workspace.relative_to(ROOT)
        command = str(payload["command"]).strip()
        timeout_ms = int(payload["timeout_ms"])
        max_output_chars = int(payload["max_output_chars"])
        if not command or timeout_ms <= 0 or max_output_chars <= 0:
            raise ValueError("invalid command settings")
    except (KeyError, TypeError, ValueError, OSError):
        await _respond(send, 400, {"error": "invalid_request"})
        return
    if path == "/run-stream":
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/x-ndjson"),
                    (b"cache-control", b"no-cache"),
                ],
            }
        )
        async for item in stream_command(command, workspace, timeout_ms, max_output_chars):
            body = (json.dumps(item, separators=(",", ":")) + "\n").encode()
            await send({"type": "http.response.body", "body": body, "more_body": True})
        await send({"type": "http.response.body", "body": b""})
    else:
        result = await run_command(command, workspace, timeout_ms, max_output_chars)
        await _respond(send, 200, result)


async def run_command(
    command: str, workspace: Path, timeout_ms: int, max_output_chars: int
) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
    except TimeoutError:
        timed_out = True
        _kill_process_group(process)
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        _kill_process_group(process)
        await process.wait()
        raise
    text = output.decode(errors="replace")[-max_output_chars:]
    exit_code = process.returncode if process.returncode is not None else -1
    if timed_out:
        exit_code = 124
        text = f"Timed out after {timeout_ms}ms.\n{text}"
    return {"exit_code": exit_code, "output": text}


async def stream_command(command: str, workspace: Path, timeout_ms: int, max_output_chars: int):
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout
    captured = ""
    timed_out = False
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            while chunk := await process.stdout.read(2048):
                text = chunk.decode(errors="replace")
                captured = f"{captured}{text}"[-max_output_chars:]
                yield {"type": "output", "text": text}
            await process.wait()
    except TimeoutError:
        timed_out = True
        _kill_process_group(process)
        await process.wait()
    except asyncio.CancelledError:
        _kill_process_group(process)
        await process.wait()
        raise
    finally:
        if process.returncode is None:
            _kill_process_group(process)
            await process.wait()
    exit_code = process.returncode if process.returncode is not None else -1
    if timed_out:
        exit_code = 124
        captured = f"Timed out after {timeout_ms}ms.\n{captured}"
    yield {"type": "result", "exit_code": exit_code, "output": captured}


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def _respond(send: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    uvicorn.run(
        "tempo.validation_server:application",
        host="0.0.0.0",
        port=int(os.getenv("TEMPO_VALIDATION_RUNNER_PORT", "8787")),
        access_log=False,
    )
