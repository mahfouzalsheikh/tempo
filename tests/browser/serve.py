"""Loopback-only UI fixture host. Bypasses auth middleware, never starts the scheduler.

Use only for tests/browser/overview.cjs, which intercepts every application API.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.urls import resolve  # noqa: E402

from tempo_web.views import dashboard, login_page, static_asset  # noqa: E402


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        request = RequestFactory().get(self.path)
        request.user = AnonymousUser()
        request.resolver_match = resolve(path)
        if path == "/":
            response = dashboard(request)
        elif path == "/login/":
            response = login_page(request)
        elif path.startswith("/static/"):
            import asyncio

            response = asyncio.run(static_asset(request, path.removeprefix("/static/")))
        else:
            self.send_error(404)
            return
        self.send_response(response.status_code)
        for name, value in response.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.content)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8049), FixtureHandler).serve_forever()
