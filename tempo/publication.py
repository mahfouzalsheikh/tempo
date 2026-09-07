"""Host-controlled publication of a committed candidate to one durable run branch."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from .credentials import process_environment
from .errors import TrackerError
from .process import stop_process_group
from .validation import clean_workspace_head, workspace_fingerprint


def publication_error(message: str) -> TrackerError:
    return TrackerError(message, category="publication_conflict")


async def git_command(cwd, *arguments, env=None, input=None, stdout=asyncio.subprocess.PIPE):
    process = await asyncio.create_subprocess_exec(
        "git",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        *arguments,
        cwd=cwd,
        env=env if env is not None else process_environment(),
        stdin=asyncio.subprocess.PIPE,
        stdout=stdout,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(input), timeout=120)
    except BaseException:
        await stop_process_group(process)
        raise
    if process.returncode:
        # Credential-bearing Git errors must never enter model context or run history.
        raise publication_error("Git publication command failed; inspect the remote and retry.")
    return (output or b"").strip()


async def push_candidate(workspace, sha, branch, previous, remote, token, ownership_check):
    """Copy objects first; give credentials only to a new, host-owned bare repository."""
    with tempfile.TemporaryDirectory(prefix="tempo-publish-") as directory:
        root = Path(directory)
        env = {
            "PATH": os.defpath,
            "HOME": directory,
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
        pack = root / "candidate.pack"
        with pack.open("wb") as output:
            await git_command(
                workspace,
                "pack-objects",
                "--revs",
                "--stdout",
                env=env,
                input=f"{sha}\n".encode(),
                stdout=output,
            )
        bare = root / "repo.git"
        await git_command(root, "init", "--bare", "--template=", str(bare), env=env)
        pack_hash = (await git_command(bare, "index-pack", "--strict", str(pack), env=env)).decode()
        # index-pack writes beside the input pack; move both into the isolated object store.
        for path in (pack, pack.with_suffix(".idx")):
            path.rename(bare / "objects" / "pack" / f"pack-{pack_hash}{path.suffix}")
        if previous:
            await git_command(bare, "merge-base", "--is-ancestor", previous, sha, env=env)
        await ownership_check()
        if token:
            credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {credential}",
            )
        await git_command(
            bare,
            "-c",
            "http.followRedirects=false",
            "-c",
            "credential.helper=",
            "push",
            "--porcelain",
            "--no-verify",
            f"--force-with-lease=refs/heads/{branch}:{previous or ''}",
            remote,
            f"{sha}:refs/heads/{branch}",
            env=env,
        )


class PublicationController:
    def __init__(self, tracker, workspace, state, save):
        self.tracker = tracker
        self.workspace = workspace
        self.state = state
        self.save = save
        self.lock = asyncio.Lock()

    async def record(self, **updates):
        state = {**self.state, **updates}
        await self.save(state)
        self.state = state

    async def remote_head(self):
        try:
            row = await self.tracker._request(
                "GET",
                f"/repos/{self.tracker.repo}/git/ref/heads/{self.state['branch']}",
            )
        except TrackerError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        sha = (row.get("object") or {}).get("sha") if isinstance(row, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise publication_error("GitHub did not return a valid branch identity.")
        return sha

    async def unchanged_from_base(self):
        head = await clean_workspace_head(self.workspace)
        if not head:
            return False
        try:
            before = await git_command(
                self.workspace, "rev-parse", f"{self.state['base_sha']}^{{tree}}"
            )
            after = await git_command(self.workspace, "rev-parse", f"{head}^{{tree}}")
            return before == after and head == await clean_workspace_head(self.workspace)
        except TrackerError:
            return False

    async def publish(self, arguments, issue, fingerprint):
        if set(arguments) - {"title", "body"}:
            raise publication_error(
                "github_publish accepts only title and body; Tempo owns the target."
            )
        title, body = arguments.get("title"), arguments.get("body", "")
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
            raise publication_error("A nonempty title and a text body are required.")
        async with self.lock:
            await self.tracker.assert_ownership()
            sha = await clean_workspace_head(self.workspace)
            if not sha or (
                fingerprint and fingerprint != await workspace_fingerprint(self.workspace)
            ):
                raise publication_error(
                    "Commit all changes and validate the clean candidate first."
                )
            if sha != await clean_workspace_head(self.workspace):
                raise publication_error("The candidate changed while checking validation.")
            await git_command(
                self.workspace, "merge-base", "--is-ancestor", self.state["base_sha"], sha
            )
            intent = self.state.get("intent")
            remote_sha = await self.remote_head()
            if intent and intent.get("status") == "pending":
                # Settle the previous intent before considering a different candidate.
                if remote_sha == intent["sha"]:
                    await self.record(
                        published_sha=remote_sha, intent={**intent, "status": "pushed"}
                    )
                elif remote_sha != intent["previous"] or sha != intent["sha"]:
                    raise publication_error(
                        "A pending push must be reconciled before another candidate."
                    )
            previous = self.state.get("published_sha")
            if remote_sha != previous:
                raise publication_error(
                    "The run branch changed outside Tempo; publication is blocked."
                )
            if remote_sha != sha:
                intent = {
                    "sha": sha,
                    "previous": previous,
                    "status": "pending",
                    "validation_fingerprint": fingerprint,
                    "validation_policy_digest": getattr(
                        self.tracker, "_validation_policy_digest", None,
                    ),
                }
                await self.record(intent=intent)
                try:
                    await push_candidate(
                        self.workspace,
                        sha,
                        self.state["branch"],
                        previous,
                        self.tracker.git_remote_url,
                        self.tracker.token,
                        self.tracker.assert_ownership,
                    )
                except TrackerError:
                    if await self.remote_head() != sha:
                        raise
                if await self.remote_head() != sha:
                    raise publication_error("Could not confirm the published commit.")
                await self.record(published_sha=sha, intent={**intent, "status": "pushed"})
            marker = self.state["marker"]
            description = f"{body.rstrip()}\n\nCloses #{issue.id}\n\n{marker}".strip()
            pr_intent = self.state.get("pr_intent")
            if not pr_intent:
                pr_intent = {"title": title.strip(), "body": description}
                await self.record(pr_intent=pr_intent)
            pr = await self.find_pull_request(sha)
            if pr is None:
                try:
                    pr = await self.tracker._request(
                        "POST",
                        f"/repos/{self.tracker.repo}/pulls",
                        json={
                            **pr_intent,
                            "head": self.state["branch"],
                            "base": self.state["base_branch"],
                            "maintainer_can_modify": False,
                        },
                    )
                except TrackerError:
                    pr = await self.find_pull_request(sha)
                    if pr is None:
                        raise
            self.check_pull_request(pr, sha)
            await self.record(pull_request={"number": pr["number"], "html_url": pr["html_url"]})
            return {**self.state["pull_request"], "head_sha": sha, "branch": self.state["branch"]}

    def check_pull_request(self, pr, sha):
        if not isinstance(pr, dict):
            raise publication_error("GitHub returned an invalid pull request.")
        head, base = pr.get("head") or {}, pr.get("base") or {}
        if (
            head.get("sha") != sha
            or head.get("ref") != self.state["branch"]
            or (head.get("repo") or {}).get("full_name") != self.tracker.repo
            or base.get("ref") != self.state["base_branch"]
            or (base.get("repo") or {}).get("full_name") != self.tracker.repo
            or self.state["marker"] not in str(pr.get("body", ""))
            or not isinstance(pr.get("number"), int)
            or not pr.get("html_url")
            or pr.get("state") != "open"
        ):
            raise publication_error(
                "The pull request does not match this run's publication intent."
            )

    async def find_pull_request(self, sha):
        number = (self.state.get("pull_request") or {}).get("number")
        if number:
            rows = [
                await self.tracker._request("GET", f"/repos/{self.tracker.repo}/pulls/{number}")
            ]
        else:
            rows = await self.tracker._request(
                "GET",
                f"/repos/{self.tracker.repo}/pulls",
                params={
                    "state": "all",
                    "head": f"{self.tracker.repo.split('/')[0]}:{self.state['branch']}",
                    "per_page": 100,
                },
            )
        if not isinstance(rows, list) or len(rows) > 1:
            raise publication_error("Ambiguous pull requests for the run branch.")
        if rows:
            self.check_pull_request(rows[0], sha)
            return rows[0]
        return None


async def prepare_publication(tracker, workspace, run_id, state, save):
    if state is None:
        repository = await tracker._request("GET", f"/repos/{tracker.repo}")
        base = repository.get("default_branch")
        if not isinstance(base, str) or not base:
            raise publication_error(
                "A default branch is required before starting a publication run."
            )
        ref = await tracker._request(
            "GET", f"/repos/{tracker.repo}/git/ref/heads/{quote(base, safe='/')}"
        )
        sha = (ref.get("object") or {}).get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise publication_error("Could not capture the task's base commit.")
        state = {
            "repo": tracker.repo,
            "branch": f"tempo/run-{run_id}",
            "base_branch": base,
            "base_sha": sha,
            "marker": f"<!-- tempo-run:{run_id} -->",
        }
        await save(state)
    if state.get("repo") != tracker.repo or state.get("branch") != f"tempo/run-{run_id}":
        raise publication_error(
            "The durable publication target differs from this run's repository."
        )
    return PublicationController(tracker, workspace, state, save)
