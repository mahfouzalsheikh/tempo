"""Run inside the deployed Tempo service; credentials never leave this process."""

import json
import os
import shlex
import tempfile
import urllib.request
from pathlib import Path

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
