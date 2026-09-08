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
from .credentials import CONTROL_PLANE_SECRETS, process_environment
from .errors import ConfigError
from .process import stop_process_group
from .run_snapshot import PINNED_EXECUTION, execution_setting
from .validation_auth import runner_token
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


async def workspace_fingerprint(workspace: Path, *, env=None) -> str:
    """Hash tracked and untracked workspace content independently of Git metadata."""
    process = await asyncio.create_subprocess_exec(
        "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.ext.allow=never",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        cwd=workspace,
        env=env if env is not None else process_environment(),
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


async def commit_fingerprint(workspace: Path, sha: str, *, env=None) -> str | None:
    """Hash the actual Git blobs, independent of index flags, timestamps and replace refs."""
    process = await asyncio.create_subprocess_exec(
        "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.ext.allow=never",
        "--no-replace-objects",
        "ls-tree",
        "-rz",
        "--full-tree",
        sha,
        cwd=workspace,
        env=env if env is not None else process_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
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
        "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.ext.allow=never",
        "--no-replace-objects",
        "cat-file",
        "--batch",
        cwd=workspace,
        env=env if env is not None else process_environment(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
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
        "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.ext.allow=never",
        "status",
        "--porcelain",
        cwd=workspace,
        env=process_environment(),
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
            "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.ext.allow=never",
            "rev-parse",
            revision,
            cwd=workspace,
            env=process_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            return True
        revisions.append(output.strip())
    return revisions[0] != revisions[1]


async def clean_workspace_head(workspace: Path, *, env=None) -> str | None:
    """Return the committed candidate only when the checkout is clean and stable."""

    async def git(*arguments: str) -> bytes | None:
        process = await asyncio.create_subprocess_exec(
            "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.ext.allow=never",
            "--no-replace-objects",
            *arguments,
            cwd=workspace,
            env=env if env is not None else process_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        return output.strip() if process.returncode == 0 else None

    before = await git("rev-parse", "--verify", "HEAD^{commit}")
    if not before:
        return None
    status = await git("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    if status != b"":
        return None
    committed = await commit_fingerprint(workspace, before.decode(), env=env)
    if await workspace_fingerprint(workspace, env=env) != committed:
        return None
    after = await git("rev-parse", "--verify", "HEAD^{commit}")
    return before.decode("ascii") if before == after else None


class ProjectValidator:
    """Runs policy-owned checks before agent-supplied supplemental commands."""

    def __init__(
        self,
        config: ValidationConfig,
        workspace_manager: WorkspaceManager,
        on_event: EventCallback,
        secret_names: set[str],
        *,
        after_checks=None,
    ) -> None:
        self.config = config
        self.workspace_manager = workspace_manager
        self.on_event = on_event
        self.after_checks = after_checks
        self.secret_names = secret_names | {
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
            "DJANGO_SECRET_KEY",
            "TEMPO_ADMIN_PASSWORD",
        }
        self.runner_url = config.runner_url or execution_setting("TEMPO_VALIDATION_RUNNER_URL")

    @staticmethod
    def tool_spec() -> dict[str, Any]:
        return {
            "name": "project_validation",
            "description": (
                "Run the operator-configured required checks, followed by optional extra commands. "
                "Use normal workspace shell and file tools for discovery, inspection, editing, "
                "and focused development checks; those activities are not validation attempts. "
                "Required checks cannot be replaced or omitted. Supply extra checks if needed. "
                "Publication requires every executed check and cleanup to succeed."
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
                        "minItems": 0,
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
                "required": ["summary"],
                "additionalProperties": False,
            },
        }

    async def execute(self, arguments: dict[str, Any], workspace: Path) -> dict[str, Any]:
        self.workspace_manager.assert_contained(workspace)
        # Freeze the policy for this attempt, including host-selected commands and timeouts.
        policy = self.config.model_copy(deep=True)
        digest = policy.policy_digest
        self.runner_url = policy.runner_url or execution_setting("TEMPO_VALIDATION_RUNNER_URL")
        if policy.missing_policy:
            return self._tool_result(
                False,
                "No required validation checks are configured. "
                "An operator must configure validation.required_checks.",
            )
        if not isinstance(arguments, dict) or set(arguments) - {
            "summary",
            "commands",
            "cleanup_command",
        }:
            return self._tool_result(
                False, "Only summary, commands and cleanup_command are accepted"
            )
        summary = arguments.get("summary", "")
        raw_commands = arguments.get("commands", [])
        cleanup = arguments.get("cleanup_command", "")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or not isinstance(raw_commands, list)
        ):
            return self._tool_result(
                False, "A text summary and an optional commands list are required"
            )
        if not isinstance(cleanup, str):
            return self._tool_result(False, "cleanup_command must be text")
        if not policy.required_checks and not raw_commands:
            return self._tool_result(False, "Discovery mode requires at least one command")
        if len(raw_commands) > policy.max_commands:
            return self._tool_result(
                False, f"validation accepts at most {policy.max_commands} extra commands"
            )
        commands = [
            (check.name, check.command, check.timeout_ms or policy.command_timeout_ms, check.id)
            for check in policy.required_checks
        ]
        for index, row in enumerate(raw_commands, 1):
            if not isinstance(row, dict) or set(row) != {"name", "command"}:
                return self._tool_result(
                    False, f"extra command {index} requires only name and command"
                )
            name, command = row["name"], row["command"]
            if not all(isinstance(value, str) and value.strip() for value in (name, command)):
                return self._tool_result(
                    False, f"extra command {index} requires text name and command"
                )
            commands.append((name.strip(), command.strip(), policy.command_timeout_ms, None))

        required_ids = [check.id for check in policy.required_checks]
        await self.on_event(
            {
                "event": "validation_started",
                "summary": summary,
                "policy_digest": digest,
                "required_check_ids": required_ids,
            }
        )
        results: list[dict[str, Any]] = []
        passed = True
        capture_error = None
        try:
            for name, command, timeout_ms, check_id in commands:
                result = await self._run_command(
                    name, command, workspace, timeout_ms, check_id=check_id
                )
                results.append(result)
                if result["exit_code"] != 0:
                    passed = False
                    break
            if passed and self.after_checks:
                # Host-owned capture occurs after command containers stop and before cleanup.
                try:
                    await self.after_checks()
                except Exception as exc:
                    passed = False
                    capture_error = exc
        finally:
            # Policy cleanup remains last and cannot be replaced by model arguments.
            try:
                if cleanup.strip():
                    results.append(
                        await self._run_command(
                            "Agent cleanup",
                            cleanup,
                            workspace,
                            policy.cleanup_timeout_ms,
                            cleanup=True,
                        )
                    )
            finally:
                if policy.cleanup_command:
                    results.append(
                        await self._run_command(
                            "Policy cleanup",
                            policy.cleanup_command,
                            workspace,
                            policy.cleanup_timeout_ms,
                            cleanup=True,
                        )
                    )
        passed = passed and all(
            type(row["exit_code"]) is int and row["exit_code"] == 0 for row in results
        )
        passed = passed and self.config.policy_digest == digest
        evidence = {
            "policy_digest": digest,
            "required_check_ids": required_ids,
            "commands": results,
        }
        await self.on_event(
            {"event": "validation_completed", "success": passed, "summary": summary, **evidence}
        )
        details = "\n\n".join(
            f"{row['name']} (exit {row['exit_code']}):\n{row['output']}" for row in results
        )
        message = f"Local project validation {'passed' if passed else 'failed'}.\n\n{details}"
        if capture_error:
            raise capture_error
        return {**self._tool_result(passed, message), **evidence}

    async def _run_command(
        self,
        name: str,
        command: str,
        workspace: Path,
        timeout_ms: int,
        *,
        cleanup: bool = False,
        check_id: str | None = None,
    ) -> dict[str, Any]:
        await self.on_event(
            {
                "event": "validation_command_started",
                "name": name,
                "command": command,
                "cleanup": cleanup,
                "check_id": check_id,
            }
        )
        if self.runner_url:
            async with asyncio.timeout(timeout_ms / 1000 + 10):
                exit_code, text = await self._run_remote(name, command, workspace, timeout_ms)
        else:
            exit_code, text = await self._run_local(command, workspace, timeout_ms)
        result = {
            "name": name,
            "command": command,
            "exit_code": exit_code,
            "output": text,
            "cleanup": cleanup,
            "check_id": check_id,
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
        credential = runner_token(self.config.runner_token)
        expected_image = self.config.runner_image or execution_setting("TEMPO_VALIDATION_IMAGE")
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST",
                f"{self.runner_url.rstrip('/')}/run-stream",
                headers={"Authorization": f"Bearer {credential}"},
                json={
                    "workspace": str(workspace),
                    "command": command,
                    "timeout_ms": timeout_ms,
                    "max_output_chars": self.config.max_output_chars,
                    **({"execution_image": expected_image} if expected_image else {}),
                },
            ) as response:
                if response.status_code == 409:
                    raise ConfigError(
                        "The validation runner does not provide this run's saved execution image. "
                        "Restore the exact local image and refresh the retained-image "
                        "configuration before retrying.",
                        category="snapshot_environment_changed",
                    )
                if response.status_code != 200:
                    raise RuntimeError(f"validation runner returned HTTP {response.status_code}")
                result: dict[str, Any] | None = None
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    payload = json.loads(line)
                    if not isinstance(payload, dict) or result is not None:
                        raise RuntimeError("validation runner returned an invalid result stream")
                    if payload.get("type") == "output":
                        if not isinstance(payload.get("text"), str):
                            raise RuntimeError("validation runner output must be text")
                        await self.on_event(
                            {
                                "event": "validation_command_output_delta",
                                "name": name,
                                "delta": str(payload.get("text", "")),
                            }
                        )
                    elif payload.get("type") == "result":
                        result = payload
                    else:
                        raise RuntimeError("validation runner returned an unknown event")
        if not result:
            raise RuntimeError("validation runner ended without a result")
        if type(result.get("exit_code")) is not int or not isinstance(result.get("output"), str):
            raise RuntimeError("validation runner returned an invalid result")
        if result.get("execution_image") != expected_image:
            raise RuntimeError("validation evidence belongs to a different execution image")
        return result["exit_code"], result["output"]

    async def _run_local(self, command: str, workspace: Path, timeout_ms: int) -> tuple[int, str]:
        if (PINNED_EXECUTION.get() is not None
                and execution_setting("TEMPO_RUNTIME_BACKEND", "process") == "docker"):
            raise ConfigError(
                "Container runs require a configured validation runner; local fallback is blocked.",
                category="snapshot_environment_changed",
            )
        environment = process_environment(forbidden=CONTROL_PLANE_SECRETS | self.secret_names)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "--noprofile", "--norc", "-c",
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
