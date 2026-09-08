"""Probe deployed staging activation/rollback and isolation without product records."""

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


slot, first, second, first_op, second_op, rollback_op = [uuid.uuid4().hex for _ in range(6)]
address = compose("port", "release-server", "8080")
host, port_text = address.rsplit(":", 1)
assert host == "127.0.0.1", "Staging port must bind only to loopback"
port = int(port_text)
context = (
    f"SLOT={slot!r}; FIRST={first!r}; SECOND={second!r}; FIRST_OP={first_op!r}\n"
    f"SECOND_OP={second_op!r}; ROLLBACK_OP={rollback_op!r}\n"
    "import os\nfrom tempo.release_files import prepare, activate\n"
    "ROOT=os.environ['TEMPO_RELEASE_ROOT']\n"
)


def execute(source):
    return compose(
        "exec", "-T", "--user", "10001:10001", "tempo", "python", "-", source=context + source
    )


def request(path="/", headers=None):
    client = HTTPConnection(host, port, timeout=10)
    try:
        client.request("GET", path, headers={"Host": f"{slot}.localhost:{port}", **(headers or {})})
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


try:
    assert request()[0] == 404
    execute("""
import hashlib, io, zipfile
for release_id, body in ((FIRST, b'<h1>Staging first</h1>'), (SECOND, b'<h1>Staging second</h1>')):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('index.html', body)
    data = output.getvalue()
    prepare(ROOT, release_id, data,
            [{'path':'index.html', 'size':len(body), 'sha256':hashlib.sha256(body).hexdigest()}],
            hashlib.sha256(data).hexdigest(), 'a' * 64)
activate(ROOT, SLOT, FIRST, 'a' * 64, None, FIRST_OP)
""")
    status, headers, body = request()
    assert status == 200 and body == b"<h1>Staging first</h1>"
    assert headers["Content-Security-Policy"].startswith(
        "sandbox allow-scripts allow-same-origin allow-downloads;"
    )
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert request(headers={"Service-Worker": "script"})[0] == 403
    assert request(headers={"Host": f"localhost:{port}"})[0] == 404
    assert request("/../metadata.json")[0] == 404
    execute("activate(ROOT, SLOT, SECOND, 'a' * 64, FIRST_OP, SECOND_OP)")
    assert request()[2] == b"<h1>Staging second</h1>"
    execute("""
import uuid
try:
    activate(ROOT, SLOT, FIRST, 'a' * 64, FIRST_OP, uuid.uuid4().hex)
except ValueError:
    pass
else:
    raise AssertionError('Stale activation succeeded')
activate(ROOT, SLOT, FIRST, 'a' * 64, SECOND_OP, ROLLBACK_OP)
# A retried request after a lost response observes the same operation.
activate(ROOT, SLOT, FIRST, 'a' * 64, SECOND_OP, ROLLBACK_OP)
""")
    assert request()[2] == b"<h1>Staging first</h1>"
    compose(
        "exec",
        "-T",
        "release-worker",
        "python",
        "-",
        source=context
        + """
import hashlib, json, socket
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tempo_web.settings')
import django
django.setup()
from tempo.preview_probe import probe, expected_report
from tempo.release_files import document
from tempo.rollback_rehearsal import endpoint
from tempo.release_probe import absent, absent_report
host, port = endpoint()
files = list(document(ROOT, SLOT)['files'].values())
public_port = int(os.environ['TEMPO_RELEASE_PORT'])
assert probe(host, port, public_port, SLOT, files) == expected_report(files)
assert absent(host, port, int(os.environ['TEMPO_RELEASE_PORT']), ROLLBACK_OP) == absent_report()
assert os.getuid() == 10001
lines = Path('/proc/1/status').read_text().splitlines()
fields = dict(line.split(':',1) for line in lines if ':' in line)
caps = ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')
assert all(int(fields[name].strip(),16)==0 for name in caps)
assert not any(key.startswith(('GITHUB_', 'OPENAI_', 'DOCKER_')) for key in os.environ)
assert not Path('/data/workspaces').exists() and not Path('/home/tempo/.codex').exists()
print('Release worker verified serving and withdrawal HTTP checks without coding credentials.')
""",
    )
    release_id = compose("ps", "-q", "release-server")
    release = json.loads(subprocess.check_output(["docker", "inspect", release_id], text=True))[0]
    assert release["HostConfig"]["ReadonlyRootfs"]
    assert "ALL" in release["HostConfig"]["CapDrop"]
    assert any("no-new-privileges" in option for option in release["HostConfig"]["SecurityOpt"])
    assert len(release["Mounts"]) == 1 and not release["Mounts"][0]["RW"]
    assert release["Mounts"][0]["Destination"] == "/data/releases"
    networks = release["NetworkSettings"]["Networks"]
    assert len(networks) == 1
    network = json.loads(
        subprocess.check_output(
            ["docker", "network", "inspect", next(iter(networks))],
            text=True,
        )
    )[0]
    worker_id = compose("ps", "-q", "release-worker")
    assert set(network["Containers"]) == {release_id, worker_id}

    targets = [("1.1.1.1", 443)]
    for service, target_port in (
        ("tempo", 8000),
        ("postgres", 5432),
        ("project-runner", 2375),
        ("acceptance-worker", 8000),
        ("preview-server", 8080),
        ("release-worker", 8000),
    ):
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
        "release-server",
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
    Path('/data/releases/write-probe').write_text('unexpected')
except OSError:
    pass
else:
    raise AssertionError('Staging storage is writable')
for host, port in TARGETS:
    try:
        connection = socket.create_connection((host, port), timeout=1)
    except OSError:
        continue
    connection.close()
    raise AssertionError('Staging server can reach forbidden infrastructure')
try:
    socket.getaddrinfo('example.com', 443)
except OSError:
    pass
else:
    raise AssertionError('Staging server can use outbound DNS')
""",
    )
    worker = json.loads(subprocess.check_output(["docker", "inspect", worker_id], text=True))[0]
    assert worker["HostConfig"]["ReadonlyRootfs"] and worker["HostConfig"]["CapDrop"] == ["ALL"]
    mounts = {item["Destination"]: item["RW"] for item in worker["Mounts"]}
    assert mounts == {"/data/releases": True, "/data/previews": False}
    assert len(worker["NetworkSettings"]["Networks"]) == 2
    forbidden = []
    for service, destination_port in (
        ("tempo", 8000),
        ("project-runner", 2375),
        ("validation-runner", 8787),
    ):
        container = json.loads(
            subprocess.check_output(["docker", "inspect", compose("ps", "-q", service)], text=True)
        )[0]
        forbidden.extend(
            (item["IPAddress"], destination_port)
            for item in container["NetworkSettings"]["Networks"].values()
        )
    compose(
        "exec",
        "-T",
        "release-worker",
        "python",
        "-",
        source=f"TARGETS={forbidden!r}\n"
        + """
import socket
for host, port in TARGETS:
    try:
        client=socket.create_connection((host,port),timeout=1)
    except OSError:
        continue
    client.close()
    raise AssertionError('Release worker can reach the control API or execution daemon')
""",
    )
finally:
    execute("""
import shutil
from pathlib import Path
root = Path(ROOT)
(root / 'targets' / f'{SLOT}.json').unlink(missing_ok=True)
for release_id in (FIRST, SECOND):
    shutil.rmtree(root / 'bundles' / release_id, ignore_errors=True)
(root / 'locks' / f'{SLOT}.lock').unlink(missing_ok=True)
""")

assert request()[0] == 404, "Temporary staging target survived cleanup"
print(
    "Staging activation, rollback, stale-operation fencing, browser policy, "
    "read-only storage, and network probes passed."
)
