"""Trusted image-owned browser harness; never import code from the application archive."""

import json
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from tempo.acceptance_contract import RUNNER, digest, verify_specification
from tempo.preview_files import publish
from tempo.preview_server import PreviewHandler


def locator(page, kind, name):
    if kind == "text":
        return page.get_by_text(name, exact=True)
    if kind == "testid":
        return page.get_by_test_id(name)
    return page.get_by_role(kind, name=name, exact=True)


def run(inputs):
    suite = verify_specification(
        inputs["suite"], inputs["suite_digest"], inputs["brief_digest"], inputs["criteria"]
    )
    token = "a" * 32
    root = Path("/tmp/acceptance-preview")
    publish(
        root,
        token,
        Path("/input/artifact.zip").read_bytes(),
        inputs["files"],
        inputs["artifact_digest"],
        time.time() + 180,
    )
    server = HTTPServer(("127.0.0.1", 8080), PreviewHandler)
    server.root, server.public_port = root, 8080
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://{token}.localhost:8080"
    results = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--host-resolver-rules=MAP *.localhost 127.0.0.1"],
            )
            version = browser.version
            for check in suite["checks"]:
                result = {"id": check["id"], "status": "passed", "steps": [], "error": ""}
                context = browser.new_context(service_workers="block", accept_downloads=False)
                context.set_default_timeout(5000)
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if urlsplit(route.request.url).netloc == origin.split("//")[1]
                        and urlsplit(route.request.url).scheme == "http"
                        else route.abort()
                    ),
                )
                page = context.new_page()
                try:
                    page.goto(origin + "/", wait_until="domcontentloaded", timeout=15000)
                    for action in check["steps"]:
                        step = {"action": action, "status": "failed"}
                        result["steps"].append(step)
                        if action[0] == "open":
                            page.goto(
                                origin + action[1], wait_until="domcontentloaded", timeout=15000
                            )
                        elif action[0] == "fill":
                            page.get_by_label(action[1], exact=True).fill(action[2])
                        elif action[0] == "click":
                            locator(page, action[1], action[2]).click()
                        else:
                            target = locator(page, action[1], action[2])
                            expect(target).to_be_visible(timeout=5000)
                            if len(action) == 4:
                                expect(target).to_have_text(action[3], timeout=5000)
                        step["status"] = "passed"
                except Exception as error:
                    result.update(status="failed", error=str(error)[:2000])
                finally:
                    context.close()
                results.append(result)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return {
        "schema": 1,
        "runner": RUNNER,
        "suite_digest": digest(suite),
        "artifact_digest": inputs["artifact_digest"],
        "browser": version,
        "results": results,
    }


if __name__ == "__main__":
    print(json.dumps(run(json.loads(Path("/input/checks.json").read_text()))))
