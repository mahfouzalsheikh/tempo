"""Private contributor repositories and a durable, serialized integration queue."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shutil
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from .errors import WorkspaceError
from .publication import git_command
from .validation import clean_workspace_head
from .workspace import contribution_root


def integration_error(message: str) -> WorkspaceError:
    return WorkspaceError(message, category="integration_conflict")


def git_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_AUTHOR_NAME": "Tempo", "GIT_AUTHOR_EMAIL": "tempo@localhost",
        "GIT_COMMITTER_NAME": "Tempo", "GIT_COMMITTER_EMAIL": "tempo@localhost",
    }


async def git(path: Path, *args: str, **kwargs) -> bytes:
    try:
        return await git_command(path, *args, env=git_environment(), **kwargs)
    except Exception as exc:
        raise integration_error(
            "Git integration failed; the contribution is retained for inspection."
        ) from exc


def check_metadata_files(path: Path) -> None:
    """Do not let an agent redirect host Git into another repository or object store."""
    metadata = path / ".git"
    if path.is_symlink() or metadata.is_symlink() or not metadata.is_dir():
        raise integration_error("Integration requires a standalone Git repository.")
    for directory, dirs, files in os.walk(metadata, followlinks=False):
        for name in [*dirs, *files]:
            item = Path(directory) / name
            metadata_stat = item.lstat()
            if (not (stat.S_ISDIR(metadata_stat.st_mode) or stat.S_ISREG(metadata_stat.st_mode))
                    or (item.is_file() and metadata_stat.st_nlink != 1)):
                raise integration_error("Linked Git metadata is not accepted for integration.")
    for name in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts"):
        if (metadata / name).exists():
            raise integration_error("External Git object stores and grafts are not supported.")


async def inspect_repository(path: Path) -> str:
    await asyncio.to_thread(check_metadata_files, path)
    # No ambient/global configuration, includes, filters, merge drivers, executable helpers,
    # or alternate worktrees. Remote metadata is retained only in the integration checkout.
    config = await git(path, "config", "--local", "--no-includes", "--null", "--list")
    safe = re.compile(
        r"(?:core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|"
        r"precomposeunicode)|user\.(?:name|email)|init\.defaultbranch|"
        r"remote\.[^.]+\.(?:url|fetch)|branch\.[^.]+\.(?:remote|merge))"
    )
    for entry in config.split(b"\0"):
        if not entry:
            continue
        key, _, value = entry.partition(b"\n")
        if not safe.fullmatch(key.decode()) or (
            key == b"core.bare" and value.lower() != b"false"
        ):
            raise integration_error(
                "Repository Git configuration is unsupported for controlled integration."
            )
    # clean_workspace_head also compares actual blobs/modes, independently of index flags.
    paths = await git(path, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for raw in paths.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise integration_error("Invalid repository file path.")
        if any((path / parent).is_symlink() for parent in relative.parents):
            raise integration_error("Repository file parents must not be symlinks.")
    head = await clean_workspace_head(path, env=git_environment())
    if not head or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise integration_error("Commit all contributor changes before integration.")
    return head


async def copy_commit(source: Path, destination: Path, sha: str) -> None:
    """Copy verified objects; never share writable objects, config, hooks, or credentials."""
    with tempfile.TemporaryDirectory(
        prefix=".tempo-objects-", dir=destination / ".git" / "objects" / "pack",
    ) as directory:
        pack = Path(directory) / "candidate.pack"
        with pack.open("wb") as output:
            await git(source, "pack-objects", "--revs", "--stdout",
                      input=f"{sha}\n".encode(), stdout=output)
        identity = (await git(destination, "index-pack", "--strict", str(pack))).decode()
        for file in (pack, pack.with_suffix(".idx")):
            target = destination / ".git" / "objects" / "pack" / f"pack-{identity}{file.suffix}"
            file.replace(target)


async def make_repository(source: Path, destination: Path, sha: str) -> None:
    await asyncio.to_thread(destination.mkdir, mode=0o700)
    try:
        await git(destination, "init", "--template=", "--initial-branch=tempo-contribution")
        await copy_commit(source, destination, sha)
        await git(destination, "config", "user.name", "Tempo contributor")
        await git(destination, "config", "user.email", "tempo@localhost")
        await git(destination, "reset", "--hard", sha)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, destination)
        raise


class ContributionCoordinator:
    def __init__(self, workspace: Path, run_id: str, states: dict, save, ownership_check):
        self.workspace = workspace
        self.root = contribution_root(workspace) / hashlib.sha256(run_id.encode()).hexdigest()[:24]
        self.states = states
        self.save = save
        self.ownership_check = ownership_check
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def exclusive(self):
        async with self.lock:
            if self.root.parent.is_symlink():
                raise integration_error("The contribution storage path must not be a symlink.")
            self.root.parent.mkdir(mode=0o700, exist_ok=True)
            descriptor = os.open(
                self.root.parent / ".integration.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                while True:
                    await self.ownership_check()
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                yield
            finally:
                os.close(descriptor)

    def path_for(self, node_id: str) -> Path:
        return self.root / hashlib.sha256(node_id.encode()).hexdigest()[:24]

    async def record(self, node_id: str, **updates) -> dict:
        state = {**self.states.get(node_id, {}), "node_id": node_id, **updates}
        await self.ownership_check()
        await self.save(state)
        self.states[node_id] = state
        return state

    async def prepare(self, node_id: str) -> Path:
        async with self.exclusive():
            return await self._prepare(node_id)

    async def _prepare(self, node_id: str) -> Path:
        await self.ownership_check()
        path = self.path_for(node_id)
        if node_id in self.states:
            if not path.is_dir() or path.is_symlink():
                raise integration_error("The recorded contributor workspace is missing or linked.")
            return path
        base = await inspect_repository(self.workspace)
        for parent in (self.root.parent, self.root):
            if parent.is_symlink():
                raise integration_error("The contribution storage path must not be a symlink.")
            parent.mkdir(mode=0o700, exist_ok=True)
        if path.exists():
            # A crash before the preparation checkpoint leaves unclaimed files. Preserve them.
            raise integration_error(
                "An unrecorded contributor workspace needs operator inspection."
            )
        await make_repository(self.workspace, path, base)
        await self.record(node_id, status="prepared", base_sha=base, workspace=str(path))
        return path

    async def accept(self, node_id: str, output: dict) -> dict:
        path = self.path_for(node_id)
        sha = await inspect_repository(path)
        await git(path, "merge-base", "--is-ancestor", self.states[node_id]["base_sha"], sha)
        await self.record(node_id, status="accepted", head_sha=sha, output=output)
        return await self.integrate(node_id)

    async def integrate(self, node_id: str) -> dict:
        async with self.exclusive():
            await self.ownership_check()
            state = self.states[node_id]
            current = await inspect_repository(self.workspace)
            if state["status"] == "integrated":
                await git(self.workspace, "merge-base", "--is-ancestor",
                          state["integrated_sha"], current)
                return self.result(state)
            if state["status"] == "accepted":
                path = self.path_for(node_id)
                if await inspect_repository(path) != state["head_sha"]:
                    raise integration_error("The accepted contributor commit has changed.")
                scratch = Path(tempfile.mkdtemp(prefix="merge-", dir=self.root))
                await asyncio.to_thread(scratch.rmdir)
                try:
                    await make_repository(self.workspace, scratch, current)
                    await copy_commit(path, scratch, state["head_sha"])
                    try:
                        await git(scratch, "merge", "--no-ff", "--no-edit", "-m",
                                  f"Integrate Tempo node {node_id}", state["head_sha"])
                    except WorkspaceError as exc:
                        raise integration_error(
                            f"Node {node_id} conflicts with the integration checkout. "
                            "Its changes are retained; resolve the conflict before retrying."
                        ) from exc
                    candidate = await inspect_repository(scratch)
                    await copy_commit(scratch, self.workspace, candidate)
                    state = await self.record(
                        node_id, status="integrating", previous_sha=current,
                        integrated_sha=candidate,
                    )
                finally:
                    await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
            if state["status"] != "integrating":
                raise integration_error("The contribution is not ready for integration.")
            candidate = state["integrated_sha"]
            # Persist intent before moving the checkout. A lost response can replay this exact
            # candidate; divergent/dirty checkouts stop instead of being reset or overwritten.
            current = await inspect_repository(self.workspace)
            if current == state["previous_sha"]:
                await self.ownership_check()
                await git(self.workspace, "merge", "--ff-only", candidate)
            elif current != candidate:
                raise integration_error(
                    "The integration checkout changed outside the recorded intent."
                )
            if await inspect_repository(self.workspace) != candidate:
                raise integration_error("The integrated commit could not be confirmed.")
            state = await self.record(node_id, status="integrated")
            return self.result(state)

    @staticmethod
    def result(state: dict) -> dict:
        return {**state["output"], "contribution": {
            key: state[key] for key in (
                "status", "workspace", "base_sha", "head_sha", "integrated_sha",
            )
        }}
