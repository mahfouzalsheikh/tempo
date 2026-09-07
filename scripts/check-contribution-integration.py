"""Exercise private concurrent checkouts and integration using disposable container writers."""

import asyncio
import copy
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from tempo.integration import ContributionCoordinator, git, inspect_repository
from tempo.workload import execution_backend, start_workload, stop_workload
from tempo.workspace import contribution_root

if os.getuid() == 0:
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)


async def main():
    assert execution_backend() == "docker", "Contribution smoke requires disposable containers"
    root = Path(os.environ["TEMPO_WORKSPACE_ROOT"])
    with tempfile.TemporaryDirectory(prefix="contribution-smoke-", dir=root) as directory:
        workspace = Path(directory)
        processes, saved = [], {}

        async def save(state):
            saved[state["node_id"]] = copy.deepcopy(state)

        async def ownership():
            pass

        queue = ContributionCoordinator(workspace, "smoke", {}, save, ownership)
        try:
            await git(workspace, "init", "--template=", "--initial-branch=main")
            (workspace / "base.txt").write_text("base\n")
            await git(workspace, "add", ".")
            await git(workspace, "commit", "-m", "Smoke base")
            paths = [await queue.prepare(node) for node in ("left", "right")]
            for index, node in enumerate(("left", "right")):
                program = f"""
import os, pathlib, subprocess, sys
assert os.getuid() == 10001
assert not pathlib.Path({str(workspace)!r}).exists(), 'integration checkout is visible'
assert not pathlib.Path({str(paths[1 - index])!r}).exists(), 'sibling checkout is visible'
assert not pathlib.Path('/data/agent-state').exists()
assert not os.getenv('GITHUB_TOKEN') and not os.getenv('DOCKER_HOST')
assert not subprocess.check_output(['git', 'remote'])
print('ready', flush=True)
assert sys.stdin.readline().strip() == 'go'
pathlib.Path({node + '.txt'!r}).write_text({node!r})
subprocess.run(['git', 'add', '.'], check=True, stdout=subprocess.DEVNULL)
subprocess.run(['git', 'commit', '-m', {node!r}], check=True, stdout=subprocess.DEVNULL)
"""
                process = await start_workload(
                    shlex.join(["python", "-u", "-c", program]), paths[index], {},
                    kind="hook", timeout_ms=30000,
                )
                processes.append(process)
            async with asyncio.timeout(30):
                ready = await asyncio.gather(*(p.stdout.readline() for p in processes))
                assert ready == [b"ready\n", b"ready\n"], "Writers did not overlap"
                await asyncio.gather(*(p.communicate(b"go\n") for p in processes))
            assert all(p.returncode == 0 for p in processes), "A container writer failed"
            for process in processes:
                await stop_workload(process)
            assert not (workspace / "left.txt").exists()
            await asyncio.gather(queue.accept("left", {}), queue.accept("right", {}))
            assert (workspace / "left.txt").read_text() == "left"
            assert (workspace / "right.txt").read_text() == "right"
            head = await inspect_repository(workspace)
            recovered = ContributionCoordinator(workspace, "smoke", saved, save, ownership)
            await recovered.integrate("left")
            await recovered.integrate("right")
            assert await inspect_repository(workspace) == head
        finally:
            for process in processes:
                await stop_workload(process)
            shutil.rmtree(contribution_root(workspace), ignore_errors=True)
    print("Concurrent private checkouts, serialized integration, and replay checks passed.")


asyncio.run(main())
