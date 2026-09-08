"""Serve immutable static previews. No Django, credentials, shell, or database access."""

import hashlib
import mimetypes
import os
import re
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tempo.preview_files import MAX_BYTES, metadata

CSP = (
    "sandbox allow-scripts allow-same-origin allow-downloads; default-src 'none'; "
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
    "worker-src 'self' blob:; frame-src 'none'; frame-ancestors 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'"
)


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "TempoPreview"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass  # Preview hostnames are bearer capabilities; do not log them.

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if (
            self.headers.get("Service-Worker", "").lower() == "script"
            or self.headers.get("Sec-Fetch-Dest", "").lower() == "serviceworker"
        ):
            # Persistent interception would bypass server-side expiration/revocation.
            return self.reply(403, b"Service workers are disabled", "text/plain")
        host = self.headers.get("Host", "")
        if self.path == "/healthz" and host == "127.0.0.1:8080":
            return self.reply(200, b"ok", "text/plain")
        match = re.fullmatch(r"([0-9a-f]{32})\.localhost:" + str(self.server.public_port), host)
        if not match:
            return self.reply(404, b"Preview unavailable", "text/plain")
        token = match[1]
        try:
            document = metadata(self.server.root, token)
            name = unquote(urlsplit(self.path).path).lstrip("/") or "index.html"
            if any(p.startswith(".") for p in name.split("/")) or "\\" in name:
                raise ValueError("Invalid path")
            entries = document["files"]
            if name not in entries:
                if name.rstrip("/") + "/index.html" in entries:
                    name = name.rstrip("/") + "/index.html"
                elif "." not in name.rsplit("/", 1)[-1]:
                    name = "index.html"  # Static SPA routes; missing assets remain 404.
                else:
                    raise ValueError("Missing file")
            expected = entries[name]
            if not 0 <= expected["size"] <= MAX_BYTES:
                raise ValueError("Invalid file size")
            with zipfile.ZipFile(Path(self.server.root) / token / "artifact.zip") as archive:
                if archive.getinfo(name).file_size != expected["size"]:
                    raise ValueError("File size mismatch")
                data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != expected["sha256"]:
                raise ValueError("File digest mismatch")
            # Revocation or expiration while reading also fails closed.
            if metadata(self.server.root, token) != document:
                raise ValueError("Preview changed")
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            return self.reply(404, b"Preview unavailable or expired", "text/plain")
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self.reply(200, data, content_type)

    def reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def main():
    server = HTTPServer(("0.0.0.0", 8080), PreviewHandler)
    server.root = os.environ["TEMPO_PREVIEW_ROOT"]
    server.public_port = int(os.environ.get("TEMPO_PREVIEW_PORT", "8031"))
    server.serve_forever()


if __name__ == "__main__":
    main()
