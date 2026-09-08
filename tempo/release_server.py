"""Serve the active immutable bundle for each local staging target."""

import os
import signal
import sys
from http.server import HTTPServer
from pathlib import Path

from tempo.preview_server import PreviewHandler
from tempo.release_files import document


class ReleaseHandler(PreviewHandler):
    server_version = "TempoStaging"

    def document(self, token):
        return document(self.server.root, token)

    def archive_path(self, token, value):
        return Path(self.server.root) / "bundles" / value["release_id"] / "artifact.zip"


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = HTTPServer(("0.0.0.0", 8080), ReleaseHandler)
    server.root = os.environ["TEMPO_RELEASE_ROOT"]
    server.public_port = int(os.environ.get("TEMPO_RELEASE_PORT", "8032"))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
