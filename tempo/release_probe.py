"""Bounded withdrawal observation for a temporary staging origin."""

import socket
import threading
import time
from http.client import HTTPConnection

from tempo.acceptance_contract import digest
from tempo.preview_files import TOKEN
from tempo.preview_probe import HEADERS


def absent_report():
    return {"checker": "staging-withdrawal-v1", "status": 404, "headers_digest": digest(HEADERS)}


def absent(host, port, public_port, slot):
    if not TOKEN.fullmatch(slot):
        raise ValueError("Invalid rehearsal slot")
    connection = HTTPConnection(host, port, timeout=3)
    started = time.monotonic()
    connection.connect()
    sock = connection.sock

    def interrupt():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(8, interrupt)
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", "/", headers={"Host": f"{slot}.localhost:{public_port}"})
        with connection.getresponse() as response:
            raw = response.getheaders()
            headers = {key.lower(): value for key, value in raw}
            if (
                response.status != 404
                or len(headers) != len(raw)
                or any(headers.get(key) != value for key, value in HEADERS.items())
                or "set-cookie" in headers
                or "transfer-encoding" in headers
                or headers.get("content-encoding") not in {None, "identity"}
                or headers.get("content-type") != "text/plain"
                or not 0 < int(headers.get("content-length", "0")) <= 1024
            ):
                raise ValueError("Withdrawn rehearsal remains accessible or policy changed")
            size = int(headers["content-length"])
            if len(response.read(size + 1)) != size or time.monotonic() - started >= 8:
                raise ValueError("Withdrawal response incomplete or timed out")
    finally:
        timer.cancel()
        connection.close()
    return absent_report()
