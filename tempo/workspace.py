from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path

import structlog

from .config import HooksConfig
from .credentials import process_environment, redact_credentials
from .domain import Workspace
from .errors import WorkspaceError
from .workload import EXECUTION_SECRETS, execution_backend, start_workload, stop_workload

log = structlog.get_logger(__name__)
SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")


def workspace_key(identifier: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", identifier)
    if sanitized != identifier:
        digest = hashlib.sha256(identifier.encode()).hexdigest()[:16]
        sanitized = f"{sanitized}-{digest}"
    if sanitized in {"", ".", ".."}:
        digest = hashlib.sha256(identifier.encode()).hexdigest()[:16]
        sanitized = f"issue-{digest}"
    return sanitized


class WorkspaceManager:
    def __init__(self, root: Path, hooks: HooksConfig) -> None:
        self.root = root.resolve(strict=False)
        self.hooks = hooks

    def path_for(self, identifier: str) -> Path:
        key = workspace_key(identifier)
        if not SAFE_KEY.fullmatch(key):
            raise WorkspaceError("derived workspace key is unsafe")
        path = (self.root / key).resolve(strict=False)
        self.assert_contained(path)
        if path == self.root:
            raise WorkspaceError("issue workspace cannot equal workspace root")
        return path

    def assert_contained(self, path: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                f"workspace path {path} escapes root {self.root}",
                category="invalid_workspace_cwd",
            ) from exc

    async def create(self, identifier: str) -> Workspace:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(identifier)
        created = False
        if path.exists() and not path.is_dir():
            raise WorkspaceError(f"workspace path exists and is not a directory: {path}")
        if not path.exists():
            path.mkdir()
            created = True
        self._assert_no_symlink_escape(path)
        workspace = Workspace(path=path, workspace_key=path.name, created_now=created)
        if created and self.hooks.after_create:
            try:
                await self.run_hook("after_create", self.hooks.after_create, path, fatal=True)
            except Exception:
                shutil.rmtree(path, ignore_errors=True)
                raise
        return workspace

    def _assert_no_symlink_escape(self, path: Path) -> None:
        canonical_root = self.root.resolve(strict=True)
        canonical_path = path.resolve(strict=True)
        try:
            canonical_path.relative_to(canonical_root)
        except ValueError as exc:
            raise WorkspaceError("workspace symlink escapes configured root") from exc

    async def remove(self, identifier: str) -> None:
        path = self.path_for(identifier)
        if not path.exists():
            return
        self._assert_no_symlink_escape(path)
        if self.hooks.before_remove:
            await self.run_hook("before_remove", self.hooks.before_remove, path, fatal=False)
        await asyncio.to_thread(shutil.rmtree, path)

    async def before_run(self, path: Path) -> None:
        if self.hooks.before_run:
            await self.run_hook("before_run", self.hooks.before_run, path, fatal=True)

    async def after_run(self, path: Path) -> None:
        if self.hooks.after_run:
            await self.run_hook("after_run", self.hooks.after_run, path, fatal=False)

    async def run_hook(self, name: str, script: str, cwd: Path, *, fatal: bool) -> None:
        self.assert_contained(cwd)
        await log.ainfo("workspace_hook_started", hook=name, workspace_path=str(cwd))
        environment = process_environment(self.hooks.environment, forbidden=EXECUTION_SECRETS)
        process = await start_workload(
            script, cwd, environment, kind="hook", timeout_ms=self.hooks.timeout_ms,
            stdin=None, stderr=asyncio.subprocess.STDOUT,
        )
        output = b""
        retained = 2000 + max((len(environment[key].encode())
                               for key in self.hooks.environment), default=0)
        try:
            grace = 5 if execution_backend() == "docker" else 0
            async with asyncio.timeout(self.hooks.timeout_ms / 1000 + grace):
                while chunk := await process.stdout.read(2048):
                    output = (output + chunk)[-retained:]
                await process.wait()
        except TimeoutError as exc:
            message = f"{name} hook timed out after {self.hooks.timeout_ms}ms"
            await log.awarning("workspace_hook_timeout", hook=name, workspace_path=str(cwd))
            if fatal:
                raise WorkspaceError(message, category="hook_timeout") from exc
            return
        except asyncio.CancelledError:
            raise
        finally:
            await stop_workload(process)
        if process.returncode:
            text = redact_credentials(
                output.decode(errors="replace"),
                [environment[name] for name in self.hooks.environment],
            )[-2000:]
            await log.awarning(
                "workspace_hook_failed",
                hook=name,
                workspace_path=str(cwd),
                exit_code=process.returncode,
                output=text,
            )
            if fatal:
                raise WorkspaceError(
                    f"{name} hook exited {process.returncode}: {text}",
                    category="hook_failed",
                )
