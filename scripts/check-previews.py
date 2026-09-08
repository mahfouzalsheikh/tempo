"""Exercise deployed preview storage/serving without creating product work or model calls."""

import hashlib
import json
import subprocess
import uuid
from http.client import HTTPConnection


def compose(*arguments, source=None):
    return subprocess.run(
        ["docker", "compose", *arguments],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


token = uuid.uuid4().hex
address = compose("port", "preview-server", "8080")
host, port_text = address.rsplit(":", 1)
assert host == "127.0.0.1", "Preview port must bind only to loopback"
port = int(port_text)

prepare = """
import hashlib, io, os, time, zipfile
from tempo.preview_files import publish
data = b'<h1>Tempo preview deployment probe</h1>'
output = io.BytesIO()
with zipfile.ZipFile(output, 'w') as archive:
    archive.writestr('index.html', data)
archive = output.getvalue()
publish(os.environ['TEMPO_PREVIEW_ROOT'], TOKEN, archive,
        [{'path': 'index.html', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}],
        hashlib.sha256(archive).hexdigest(), time.time() + 120)
"""


def request(path="/", headers=None):
    client = HTTPConnection(host, port, timeout=10)
    client.request("GET", path, headers={"Host": f"{token}.localhost:{port}", **(headers or {})})
    response = client.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    client.close()
    return result


try:
    compose(
        "exec",
        "-T",
        "--user",
        "10001:10001",
        "tempo",
        "python",
        "-",
        source=f"TOKEN = {token!r}\n" + prepare,
    )
    status, headers, data = request()
    assert status == 200
    assert (
        hashlib.sha256(data).digest()
        == hashlib.sha256(b"<h1>Tempo preview deployment probe</h1>").digest()
    )
    assert headers["Content-Security-Policy"].startswith(
        "sandbox allow-scripts allow-same-origin allow-downloads;"
    )
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert request(headers={"Service-Worker": "script"})[0] == 403
    assert request(headers={"Host": f"localhost:{port}"})[0] == 404
    assert request("/../metadata.json")[0] == 404

    preview_id = compose("ps", "-q", "preview-server")
    preview = json.loads(subprocess.check_output(["docker", "inspect", preview_id], text=True))[0]
    assert preview["HostConfig"]["ReadonlyRootfs"]
    assert "ALL" in preview["HostConfig"]["CapDrop"]
    assert any("no-new-privileges" in option for option in preview["HostConfig"]["SecurityOpt"])
    assert len(preview["Mounts"]) == 1 and not preview["Mounts"][0]["RW"]
    assert preview["Mounts"][0]["Destination"] == "/data/previews"
    networks = preview["NetworkSettings"]["Networks"]
    assert len(networks) == 1
    network = json.loads(
        subprocess.check_output(
            ["docker", "network", "inspect", next(iter(networks))],
            text=True,
        )
    )[0]
    assert len(network["Containers"]) == 1

    targets = [("1.1.1.1", 443)]
    for service, target_port in (("tempo", 8000), ("postgres", 5432), ("project-runner", 2375)):
        identifier = compose("ps", "-q", service)
        document = json.loads(
            subprocess.check_output(["docker", "inspect", identifier], text=True)
        )[0]
        targets.extend(
            (item["IPAddress"], target_port)
            for item in document["NetworkSettings"]["Networks"].values()
        )
    compose(
        "exec",
        "-T",
        "--user",
        "10001:10001",
        "preview-server",
        "python",
        "-",
        source=f"TARGETS = {targets!r}\n"
        + """
import os, socket
from pathlib import Path
os.environ['RES_OPTIONS'] = 'attempts:1 timeout:1'
assert os.getuid() == 10001
status = Path('/proc/1/status').read_text().splitlines()
fields = dict(line.split(':', 1) for line in status if ':' in line)
assert set(fields['Uid'].split()) == {'10001'}
assert all(int(fields[name].strip(), 16) == 0
           for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
assert not any(key.startswith(('GITHUB_', 'OPENAI_', 'DOCKER_', 'DATABASE_', 'DJANGO_'))
               for key in os.environ)
assert not Path('/data/workspaces').exists()
assert not Path('/home/tempo/.codex').exists()
try:
    Path('/data/previews/write-probe').write_text('unexpected')
except OSError:
    pass
else:
    raise AssertionError('Preview storage is writable')
for host, port in TARGETS:
    try:
        connection = socket.create_connection((host, port), timeout=1)
    except OSError:
        continue
    connection.close()
    raise AssertionError('Preview server can reach forbidden infrastructure')
try:
    socket.getaddrinfo('example.com', 443)
except OSError:
    pass
else:
    raise AssertionError('Preview server can use outbound DNS')
""",
    )
finally:
    compose(
        "exec",
        "-T",
        "--user",
        "10001:10001",
        "tempo",
        "python",
        "-",
        source=f"""
import os
from tempo.preview_files import remove
remove(os.environ['TEMPO_PREVIEW_ROOT'], {token!r})
""",
    )

assert request()[0] == 404, "Revoked preview remains accessible"
print("Preview artifact, browser policy, revocation, read-only storage, and network probes passed.")
