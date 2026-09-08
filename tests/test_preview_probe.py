import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tempo import preview_probe


@pytest.mark.parametrize(
    "failure", ["status", "redirect", "bytes", "header", "cookie", "short", "slow", "duplicate"]
)
def test_probe_rejects_bad_responses_and_does_not_follow_redirects(failure, monkeypatch):
    seen = []
    data = b"<h1>Healthy</h1>"
    files = [{"path": "index.html", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen.append(self.path)
            try:
                if failure == "slow":
                    time.sleep(0.4)
                self.send_response(
                    302 if failure == "redirect" else 500 if failure == "status" else 200
                )
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                if failure == "duplicate":
                    self.send_header("Content-Length", str(len(data) + 100))
                if failure == "redirect":
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                if failure == "cookie":
                    self.send_header("Set-Cookie", "not-allowed=1")
                for key, value in preview_probe.HEADERS.items():
                    if failure == "header" and key == "content-security-policy":
                        continue
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(
                    b"x" * len(data)
                    if failure == "bytes"
                    else data[:3]
                    if failure == "short"
                    else data
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(preview_probe, "TIMEOUT", 0.15 if failure == "slow" else 3)
    started = time.monotonic()
    try:
        with pytest.raises((ValueError, OSError)):
            preview_probe.probe("127.0.0.1", server.server_port, 8031, "a" * 32, files)
        assert seen == ["/"]
        assert time.monotonic() - started < 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
