import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tempo import release_probe
from tempo.preview_probe import HEADERS


@pytest.mark.parametrize(
    "mode", ["valid", "accessible", "redirect", "cookie", "duplicate", "large", "slow"]
)
def test_withdrawal_requires_bounded_private_404_response(mode, monkeypatch):
    original_timer = threading.Timer
    if mode == "slow":
        monkeypatch.setattr(release_probe.threading, "Timer", lambda _, fn: original_timer(0.1, fn))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if mode == "slow":
                time.sleep(0.3)
                return
            self.send_response(200 if mode == "accessible" else 302 if mode == "redirect" else 404)
            for key, value in HEADERS.items():
                self.send_header(key, value)
            if mode == "cookie":
                self.send_header("Set-Cookie", "unwanted=true")
            if mode == "duplicate":
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2048" if mode == "large" else "7")
            self.end_headers()
            self.wfile.write(b"missing")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if mode == "valid":
            assert (
                release_probe.absent("127.0.0.1", server.server_port, 8032, "a" * 32)
                == release_probe.absent_report()
            )
        else:
            with pytest.raises((ValueError, OSError)):
                release_probe.absent("127.0.0.1", server.server_port, 8032, "a" * 32)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
