"""Run inside the deployed Tempo service; credentials never leave this process."""

import json
import os
import shlex
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tempo.validation_auth import runner_environment

image = os.environ["TEMPO_VALIDATION_IMAGE"]
runner = os.environ["TEMPO_VALIDATION_RUNNER_URL"].rstrip("/")
with urllib.request.urlopen(f"{runner}/healthz", timeout=5) as response:
    health = json.load(response)
assert health["backend"] == "docker" and health["image"] == image, health

with tempfile.TemporaryDirectory(
    prefix="sandbox-smoke-", dir=os.environ["TEMPO_WORKSPACE_ROOT"],
) as root:
    os.chown(root, 10001, 10001)
    task = Path(root) / "task"
    task.mkdir()
    os.chown(task, 10001, 10001)
    sibling = Path(root) / "sibling-secret"
    sibling.write_text("smoke-sentinel")
    probe = f"""
import os, pathlib, socket
assert os.getuid() == 10001
assert not pathlib.Path({str(sibling)!r}).exists()
assert not pathlib.Path('/run/tempo-host-codex').exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
keys = ('DOCKER_HOST', 'TEMPO_VALIDATION_RUNNER_TOKEN', 'GITHUB_TOKEN')
assert not any(os.getenv(key) for key in keys)
assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network access allowed')
pathlib.Path('checked').write_text('ok')
print('sandbox smoke passed')
"""
    for endpoint in ("run", "run-stream"):
        payload = {
            "workspace": str(task), "command": shlex.join(["python", "-c", probe]),
            "timeout_ms": 10000, "max_output_chars": 4000, "execution_image": image,
        }
        request = urllib.request.Request(
            f"{runner}/{endpoint}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ['TEMPO_VALIDATION_RUNNER_TOKEN']}"},
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            records = [json.loads(line) for line in response if line.strip()]
        result = records[-1]
        assert result["exit_code"] == 0, result
        assert result["execution_image"] == image, result
        assert (task / "checked").read_text() == "ok"
print("Deployed validation image and both sandbox execution endpoints verified.")

# Exercise exact historical images without retrying or modifying any product run.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
import django  # noqa: E402

django.setup()
from tempo.errors import ConfigError  # noqa: E402
from tempo.run_snapshot import restore_snapshot  # noqa: E402
from tempo.validation_images import retained_images  # noqa: E402
from tempo_web.models import AgentRun  # noqa: E402

previous = None
approved = retained_images()["images"]
saved = []
for run in AgentRun.objects.exclude(execution_snapshot={}).order_by("-pk").iterator():
    try:
        _, config = restore_snapshot(run.execution_snapshot, run.snapshot_digest)
    except ConfigError:
        continue
    saved.append(config.validation.runner_image
                 or run.execution_snapshot["execution"]["environment"]["TEMPO_VALIDATION_IMAGE"])
for candidate in dict.fromkeys([*saved, *approved]):
    if candidate not in approved:
        continue
    if candidate == image:
        continue
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", candidate],
        env=runner_environment(), capture_output=True, text=True, timeout=10,
    )
    if inspected.returncode == 0 and inspected.stdout.strip() == candidate:
        previous = candidate
        break

if previous:
    with tempfile.TemporaryDirectory(
        prefix="retained-image-smoke-", dir=os.environ["TEMPO_WORKSPACE_ROOT"],
    ) as root:
        os.chown(root, 10001, 10001)
        secret = Path(root) / "sibling-secret"
        secret.write_text("retained-smoke-sentinel")
        concurrent_probe = (
            "import time, json\nstarted = time.time()\ntime.sleep(1)\n"
            + probe.replace(str(sibling), str(secret))
            + "\nprint(json.dumps({'started': started, 'finished': time.time()}))\n"
        )

        def check(selected):
            task = Path(root) / selected.removeprefix("sha256:")
            task.mkdir()
            os.chown(task, 10001, 10001)
            payload = {
                "workspace": str(task), "command": shlex.join(["python", "-c", concurrent_probe]),
                "timeout_ms": 10000, "max_output_chars": 4000, "execution_image": selected,
            }
            request = urllib.request.Request(
                f"{runner}/run-stream", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization":
                         f"Bearer {os.environ['TEMPO_VALIDATION_RUNNER_TOKEN']}"},
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                result = [json.loads(line) for line in response if line.strip()][-1]
            assert result["exit_code"] == 0 and result["execution_image"] == selected, result
            assert (task / "checked").read_text() == "ok"
            return json.loads(result["output"].splitlines()[-1])

        with ThreadPoolExecutor(max_workers=2) as pool:
            timings = list(pool.map(check, [image, previous]))
        assert max(row["started"] for row in timings) < min(row["finished"] for row in timings)
    print(f"Concurrent current and retained image sandbox probes passed: {image}, {previous}.")
else:
    print("No additional retained local image available for the concurrent deployment probe.")
