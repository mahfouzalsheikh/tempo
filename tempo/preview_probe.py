"""Bounded HTTP checks against one operator-configured preview service; no browser or code."""

import hashlib
import mimetypes
import socket
import threading
import time
from http.client import HTTPConnection
from urllib.parse import quote

from tempo.acceptance_contract import digest
from tempo.preview_files import MAX_BYTES, MAX_FILES, TOKEN
from tempo.preview_server import CSP

VERSION = "preview-health-v1"
TIMEOUT = 45
HEADERS = {
    "content-security-policy": CSP,
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
}


def expected_report(files):
    entries = {item["path"]: item for item in files}
    return {
        "schema": 1,
        "checker": VERSION,
        "inventory_digest": digest(files),
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "root_digest": entries["index.html"]["sha256"],
        "headers_digest": digest(HEADERS),
    }


def probe(host, port, public_port, token, files):
    """No redirects, proxy environment, cookies, auth headers, or arbitrary target URLs."""
    if not TOKEN.fullmatch(token) or not 0 < len(files) <= MAX_FILES:
        raise ValueError("Invalid preview probe inputs")
    if sum(item["size"] for item in files) > MAX_BYTES:
        raise ValueError("Preview probe exceeds size limit")
    deadline = time.monotonic() + TIMEOUT
    entries = {item["path"]: item for item in files}
    # Check the user-facing root as well as every exact inventory path.
    paths = [("/", entries["index.html"])] + [
        ("/" + quote(item["path"], safe="/"), item) for item in files
    ]
    active = {}

    def interrupt():
        sock = active.get("socket")
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(TIMEOUT, interrupt)
    timer.daemon = True
    timer.start()
    try:
        for path, item in paths:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Preview probe deadline exceeded")
            connection = HTTPConnection(host, port, timeout=min(3, remaining))
            try:
                connection.connect()
                sock = connection.sock
                active["socket"] = sock
                connection.request(
                    "GET",
                    path,
                    headers={
                        "Host": f"{token}.localhost:{public_port}",
                        "Accept-Encoding": "identity",
                    },
                )
                with connection.getresponse() as response:
                    raw_headers = response.getheaders()
                    headers = {name.lower(): value for name, value in raw_headers}
                    content_type = (
                        mimetypes.guess_type(item["path"])[0] or "application/octet-stream"
                    )
                    if (
                        response.status != 200
                        or len(headers) != len(raw_headers)
                        or any(headers.get(key) != value for key, value in HEADERS.items())
                        or headers.get("content-type") != content_type
                        or headers.get("content-length") != str(item["size"])
                        or headers.get("content-encoding") not in {None, "identity"}
                        or "transfer-encoding" in headers
                        or "set-cookie" in headers
                    ):
                        raise ValueError("Preview response status or browser policy did not match")
                    received, checksum = 0, hashlib.sha256()
                    while received < item["size"]:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Preview probe deadline exceeded")
                        sock.settimeout(min(3, remaining))
                        chunk = response.read1(min(65536, item["size"] - received))
                        if not chunk:
                            break
                        received += len(chunk)
                        checksum.update(chunk)
                    if received != item["size"] or checksum.hexdigest() != item["sha256"]:
                        raise ValueError("Preview response bytes did not match the saved build")
            finally:
                active.pop("socket", None)
                connection.close()
    finally:
        timer.cancel()
    if time.monotonic() > deadline:
        raise TimeoutError("Preview probe deadline exceeded")
    return expected_report(files)
