from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .config import ValidationConfig
from .workspace import WorkspaceManager

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


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
