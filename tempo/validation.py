from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .config import ValidationConfig
from .process import stop_process_group
from .workspace import WorkspaceManager

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _fallback_workspace_paths(workspace: Path) -> list[bytes]:
    return sorted(
        str(path.relative_to(workspace)).encode()
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(workspace).parts
    )


def _fingerprint_header(digest, path: bytes, mode: bytes, size: int) -> None:
    digest.update(len(path).to_bytes(8, "big"))
    digest.update(path)
    digest.update(mode)
    digest.update(size.to_bytes(8, "big"))


async def workspace_fingerprint(workspace: Path) -> str:
    """Hash tracked and untracked workspace content independently of Git metadata."""
    process = await asyncio.create_subprocess_exec(
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    if process.returncode == 0:
        paths = sorted(path for path in output.split(b"\0") if path)
    else:
        paths = await asyncio.to_thread(_fallback_workspace_paths, workspace)
    digest = hashlib.sha256()
    for raw_path in paths:
        path = workspace / os.fsdecode(raw_path)
        try:
            if path.is_symlink():
                mode, content = b"120000", os.fsencode(os.readlink(path))
            else:
                mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
                content = await asyncio.to_thread(path.read_bytes)
        except OSError:
            mode, content = b"missing", b""
        _fingerprint_header(digest, raw_path, mode, len(content))
        digest.update(content)
    return digest.hexdigest()


async def commit_fingerprint(workspace: Path, sha: str) -> str | None:
    """Hash the actual Git blobs, independent of index flags, timestamps and replace refs."""
    process = await asyncio.create_subprocess_exec(
        "git", "--no-replace-objects", "ls-tree", "-rz", "--full-tree", sha,
        cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    if process.returncode:
        return None
    entries = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            return None  # Submodules require separate source/evidence identities.
        entries.append((path, mode, object_id))
    process = await asyncio.create_subprocess_exec(
        "git", "--no-replace-objects", "cat-file", "--batch", cwd=workspace,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    digest = hashlib.sha256()
    try:
        async with asyncio.timeout(120):
            for path, mode, object_id in sorted(entries):
                process.stdin.write(object_id + b"\n")
                await process.stdin.drain()
                header = (await process.stdout.readline()).split()
                if len(header) != 3 or header[:2] != [object_id, b"blob"]:
                    return None
                remaining = int(header[2])
                _fingerprint_header(digest, path, mode, remaining)
                while remaining:
                    chunk = await process.stdout.readexactly(min(remaining, 65536))
                    digest.update(chunk)
                    remaining -= len(chunk)
                if await process.stdout.readexactly(1) != b"\n":
                    return None
        return digest.hexdigest()
    finally:
        await stop_process_group(process)


async def workspace_publication_pending(
    workspace: Path,
    *,
    branch_ref_updated: bool = False,
) -> bool:
    """Return whether validated workspace content still needs publication."""
    status = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    status_output, _ = await status.communicate()
    if status.returncode != 0 or status_output.strip():
        return True
    if branch_ref_updated:
        return False
    revisions: list[bytes] = []
    for revision in ("HEAD", "@{upstream}"):
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            revision,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            return True
        revisions.append(output.strip())
    return revisions[0] != revisions[1]


async def clean_workspace_head(workspace: Path) -> str | None:
    """Return the committed candidate only when the checkout is clean and stable."""
    async def git(*arguments: str) -> bytes | None:
        process = await asyncio.create_subprocess_exec(
            "git", "--no-replace-objects", *arguments, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        return output.strip() if process.returncode == 0 else None

    before = await git("rev-parse", "--verify", "HEAD^{commit}")
    if not before:
        return None
    status = await git("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    if status != b"":
        return None
    committed = await commit_fingerprint(workspace, before.decode())
    if await workspace_fingerprint(workspace) != committed:
        return None
    after = await git("rev-parse", "--verify", "HEAD^{commit}")
    return before.decode("ascii") if before == after else None


class ProjectValidator:
    """Runs agent-discovered, repository-native validation commands locally."""

    def __init__(
        self,
        config: ValidationConfig,
        workspace_manager: WorkspaceManager,
        on_event: EventCallback,
        secret_names: set[str],
    ) -> None:
        self.config = config
        self.workspace_manager = workspace_manager
        self.on_event = on_event
        self.secret_names = secret_names | {
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
            "DJANGO_SECRET_KEY",
            "TEMPO_ADMIN_PASSWORD",
        }
        self.runner_url = config.runner_url or os.getenv("TEMPO_VALIDATION_RUNNER_URL")

    @staticmethod
    def tool_spec() -> dict[str, Any]:
        return {
            "name": "project_validation",
            "description": (
                "Run the repository's complete, already-discovered build and test sequence. "
                "Use normal workspace shell and file tools for discovery, inspection, editing, "
                "and focused development checks; those activities are not validation attempts. "
                "Supply all final, non-mutating validation commands in one call. Tempo executes "
                "every command and unlocks pull-request creation only when they all succeed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What project-native behavior this sequence validates.",
                    },
                    "commands": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "command": {"type": "string"},
                            },
                            "required": ["name", "command"],
                            "additionalProperties": False,
                        },
                    },
                    "cleanup_command": {
                        "type": "string",
                        "description": "Optional command always run after validation.",
                    },
                },
                "required": ["summary", "commands"],
                "additionalProperties": False,
            },
        }

    async def execute(self, arguments: dict[str, Any], workspace: Path) -> dict[str, Any]:
        self.workspace_manager.assert_contained(workspace)
        summary = str(arguments.get("summary", "")).strip()
        raw_commands = arguments.get("commands")
        if not summary or not isinstance(raw_commands, list) or not raw_commands:
            return self._tool_result(False, "summary and at least one command are required")
        if len(raw_commands) > self.config.max_commands:
            return self._tool_result(
                False, f"validation accepts at most {self.config.max_commands} commands"
            )
        commands: list[tuple[str, str]] = []
        for index, row in enumerate(raw_commands, 1):
            if not isinstance(row, dict):
                return self._tool_result(False, f"command {index} must be an object")
            name = str(row.get("name", "")).strip()
            command = str(row.get("command", "")).strip()
            if not name or not command:
                return self._tool_result(False, f"command {index} requires name and command")
            commands.append((name, command))

        await self.on_event({"event": "validation_started", "summary": summary})
        results: list[dict[str, Any]] = []
        passed = True
        try:
            for name, command in commands:
                result = await self._run_command(
                    name, command, workspace, self.config.command_timeout_ms
                )
                results.append(result)
                if result["exit_code"] != 0:
                    passed = False
                    break
        finally:
            cleanup = str(arguments.get("cleanup_command", "")).strip()
            if cleanup:
                results.append(
                    await self._run_command(
                        "Cleanup", cleanup, workspace, self.config.cleanup_timeout_ms, cleanup=True
                    )
                )

        await self.on_event(
            {
                "event": "validation_completed",
                "success": passed,
                "summary": summary,
                "commands": results,
            }
        )
        details = "\n\n".join(
            f"{row['name']} (exit {row['exit_code']}):\n{row['output']}" for row in results
        )
        message = f"Local project validation {'passed' if passed else 'failed'}.\n\n{details}"
        return self._tool_result(passed, message)

    async def _run_command(
        self,
        name: str,
        command: str,
        workspace: Path,
        timeout_ms: int,
        *,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        await self.on_event(
            {
                "event": "validation_command_started",
                "name": name,
                "command": command,
                "cleanup": cleanup,
            }
        )
        if self.runner_url:
            exit_code, text = await self._run_remote(name, command, workspace, timeout_ms)
        else:
            exit_code, text = await self._run_local(command, workspace, timeout_ms)
        result = {
            "name": name,
            "command": command,
            "exit_code": exit_code,
            "output": text,
            "cleanup": cleanup,
        }
        await self.on_event({"event": "validation_command_completed", **result})
        return result

    async def _run_remote(
        self,
        name: str,
        command: str,
        workspace: Path,
        timeout_ms: int,
    ) -> tuple[int, str]:
        timeout = httpx.Timeout(timeout_ms / 1000 + 10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.runner_url.rstrip('/')}/run-stream",
                json={
                    "workspace": str(workspace),
                    "command": command,
                    "timeout_ms": timeout_ms,
                    "max_output_chars": self.config.max_output_chars,
                },
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    raise RuntimeError(
                        f"validation runner returned {response.status_code}: {body[:500]}"
                    )
                result: dict[str, Any] | None = None
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    payload = json.loads(line)
                    if payload.get("type") == "output":
                        await self.on_event(
                            {
                                "event": "validation_command_output_delta",
                                "name": name,
                                "delta": str(payload.get("text", "")),
                            }
                        )
                    elif payload.get("type") == "result":
                        result = payload
        if not result:
            raise RuntimeError("validation runner ended without a result")
        return int(result["exit_code"]), str(result.get("output", ""))

    async def _run_local(self, command: str, workspace: Path, timeout_ms: int) -> tuple[int, str]:
        environment = os.environ.copy()
        for secret in self.secret_names:
            environment.pop(secret, None)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=workspace,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
        except TimeoutError:
            timed_out = True
            self._kill_process_group(process)
            output, _ = await process.communicate()
        except asyncio.CancelledError:
            self._kill_process_group(process)
            await process.wait()
            raise
        text = output.decode(errors="replace")[-self.config.max_output_chars :]
        exit_code = process.returncode if process.returncode is not None else -1
        if timed_out:
            exit_code = 124
            text = f"Timed out after {timeout_ms}ms.\n{text}"
        return exit_code, text

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    @staticmethod
    def _tool_result(success: bool, output: str) -> dict[str, Any]:
        return {
            "success": success,
            "output": output,
            "contentItems": [{"type": "inputText", "text": output}],
        }
