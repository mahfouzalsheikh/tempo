"""Real firewall probes in a disposable daemon; never change the host's firewall."""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def docker(*arguments, **kwargs):
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=60, **kwargs,
    )


@pytest.fixture(scope="module")
def execution_daemon():
    image = os.getenv("TEMPO_TEST_VALIDATION_IMAGE")
    if not os.getenv("TEMPO_TEST_EXECUTION_NETWORK") or not image:
        pytest.skip("Set TEMPO_TEST_EXECUTION_NETWORK=1 and TEMPO_TEST_VALIDATION_IMAGE for probes")
    identity = uuid.uuid4().hex
    daemon, network, canary = (
        f"tempo-network-test-{kind}-{identity}" for kind in ("dind", "net", "web")
    )
    try:
        docker("network", "create", network, check=True)
        docker("run", "--detach", "--name", canary, "--network", network,
               "--entrypoint", "python", image, "-m", "http.server", "443", check=True)
        docker("run", "--detach", "--privileged", "--name", daemon, "--network", network,
               "--publish", "127.0.0.1::2375", "--env", "DOCKER_TLS_CERTDIR=",
               "--mount", f"type=bind,src={ROOT / 'scripts/execution-daemon.sh'},dst=/daemon.sh,ro",
               "--entrypoint", "sh", "docker:27-dind", "/daemon.sh",
               "--host=tcp://0.0.0.0:2375", "--host=unix:///var/run/docker.sock",
               "--dns=1.1.1.1", "--dns=8.8.8.8", check=True)
        for _ in range(60):
            if docker("exec", daemon, "sh", "/daemon.sh", "--check").returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("Execution daemon did not become ready: " + docker("logs", daemon).stderr)
        docker("exec", daemon, "sh", "/daemon.sh", "--network", check=True)
        # The host and nested daemon may report different immutable image IDs.
        with tempfile.TemporaryFile() as archive:
            subprocess.run(["docker", "image", "save", image],
                           stdout=archive, check=True, timeout=60)
            archive.seek(0)
            loaded = subprocess.run(["docker", "exec", "-i", daemon, "docker", "image", "load"],
                                    stdin=archive, capture_output=True, text=True,
                                    check=True, timeout=60)
        nested_image = loaded.stdout.strip().splitlines()[-1].split(": ", 1)[1]
        nested_image = docker("exec", daemon, "docker", "image", "inspect", nested_image,
                              "--format", "{{.Id}}", check=True).stdout.strip()
        canary_ip = json.loads(docker("inspect", canary, check=True).stdout)[0][
            "NetworkSettings"]["Networks"][network]["IPAddress"]
        # Positive control: the destination is live and reachable from the daemon itself.
        docker("exec", daemon, "wget", "-q", "-O", "/dev/null",
               f"http://{canary_ip}:443", check=True)
        port = docker("port", daemon, "2375", check=True).stdout.strip()
        yield daemon, {
            "PATH": os.environ["PATH"], "DOCKER_HOST": f"tcp://{port}",
            "TEMPO_VALIDATION_IMAGE": nested_image,
            "TEMPO_NETWORK_PROBE_PRIVATE_TARGETS": json.dumps([[canary_ip, 443]]),
        }
    finally:
        for name in (daemon, canary):
            docker("rm", "--force", "--volumes", name)
        docker("network", "rm", network)


def test_private_egress_and_daemon_are_blocked_with_public_web_access(execution_daemon):
    _, environment = execution_daemon
    subprocess.run([sys.executable, str(ROOT / "scripts/check-execution-network.py")],
                   env=environment, check=True, timeout=90)


def test_policy_is_reinstalled_before_daemon_accepts_jobs_after_restart(execution_daemon):
    daemon, environment = execution_daemon
    docker("exec", daemon, "iptables", "-D", "TEMPO-JOB-OUT", "-d", "10.0.0.0/8", "-j", "REJECT",
           check=True)
    assert docker("exec", daemon, "sh", "/daemon.sh", "--check").returncode != 0
    docker("restart", "--time", "10", daemon, check=True)
    for _ in range(60):
        if docker("exec", daemon, "sh", "/daemon.sh", "--check").returncode == 0:
            break
        time.sleep(0.5)
    else:
        pytest.fail("Execution network did not recover after daemon restart")
    port = docker("port", daemon, "2375", check=True).stdout.strip()
    environment = {**environment, "DOCKER_HOST": f"tcp://{port}"}
    subprocess.run([sys.executable, str(ROOT / "scripts/check-execution-network.py")],
                   env=environment, check=True, timeout=90)
