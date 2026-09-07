"""Probe Tempo's execution daemon with disposable containers and no application credentials."""

import json
import os
import socket
import subprocess
import time
import uuid

image = os.environ["TEMPO_VALIDATION_IMAGE"]
private_targets = json.loads(os.environ.get("TEMPO_NETWORK_PROBE_PRIVATE_TARGETS", "null"))
if private_targets is None:
    private_targets = [
        [socket.gethostbyname("postgres"), 5432],
        [socket.gethostbyname("validation-runner"), 8787],
        [socket.gethostbyname(socket.gethostname()), 8000],
    ]
environment = {key: os.environ[key] for key in ("PATH", "DOCKER_HOST") if key in os.environ}


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], env=environment, capture_output=True, text=True, check=check, timeout=25,
    )


for network in ("bridge", "tempo-agents"):
    detail = json.loads(docker("network", "inspect", network).stdout)[0]
    assert not detail["EnableIPv6"]
    gateway = detail["IPAM"]["Config"][0]["Gateway"]
    identity = uuid.uuid4().hex
    peer, probe = f"tempo-network-peer-{identity}", f"tempo-network-probe-{identity}"
    options = [
        "--pull=never", "--network", network, "--read-only", "--user=10001:10001",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=32",
        "--memory=128m", "--cpus=1", "--entrypoint", "python",
    ]
    try:
        docker("run", "--detach", "--name", peer, *options, image,
               "-m", "http.server", "8080", "--bind", "0.0.0.0")
        for _ in range(20):
            ready = docker("exec", peer, "python", "-c",
                           "import socket; socket.create_connection(('127.0.0.1', 8080), 1)",
                           check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("network probe peer did not become ready")
        peer_detail = json.loads(docker("inspect", peer).stdout)[0]
        peer_ip = peer_detail["NetworkSettings"]["Networks"][network]["IPAddress"]
        targets = [[gateway, 2375], [peer_ip, 8080], *private_targets, ["169.254.169.254", 80]]
        code = f"""
import socket
for host, port in {targets!r}:
    try:
        connection = socket.create_connection((host, port), timeout=1)
    except OSError:
        continue
    connection.close()
    raise AssertionError('private execution endpoint reachable: ' + host + ':' + str(port))
assert socket.getaddrinfo('github.com', 443)
with socket.create_connection(('1.1.1.1', 443), timeout=5):
    pass
print('private destinations blocked; public DNS and HTTPS reachable')
"""
        result = docker("run", "--rm", "--name", probe, *options, image, "-c", code, check=False)
        if result.returncode:
            raise RuntimeError(f"{network} isolation probe failed: {result.stdout}{result.stderr}")
        print(f"{network}: {result.stdout.strip()}")
    finally:
        for name in (probe, peer):
            removed = docker("rm", "--force", "--volumes", name, check=False)
            if removed.returncode and "No such container" not in removed.stderr:
                raise RuntimeError("network probe cleanup could not be confirmed")
