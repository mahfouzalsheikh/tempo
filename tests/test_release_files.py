import hashlib
import io
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from tempo import release_files
from tempo.release_server import ReleaseHandler


def bundle(root, text):
    content = f"<h1>{text}</h1>".encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("index.html", content)
    data = output.getvalue()
    files = [
        {"path": "index.html", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    ]
    key = uuid.uuid4().hex
    release_files.prepare(root, key, data, files, hashlib.sha256(data).hexdigest(), "a" * 64)
    return key, data, files


def activate(root, slot, key, expected=None, operation=None):
    return release_files.activate(
        root, slot, key, "a" * 64, expected, operation or uuid.uuid4().hex
    )


def test_atomic_activation_rollback_and_lost_response_reconciliation(tmp_path):
    slot = uuid.uuid4().hex
    first, data, files = bundle(tmp_path, "First")
    second, _, _ = bundle(tmp_path, "Second")
    one = activate(tmp_path, slot, first)
    two = activate(tmp_path, slot, second, one["operation_id"])
    assert activate(tmp_path, slot, second, one["operation_id"], two["operation_id"]) == two
    with pytest.raises(ValueError, match="stale"):
        activate(tmp_path, slot, first, one["operation_id"])
    restored = activate(tmp_path, slot, first, two["operation_id"])
    assert release_files.document(tmp_path, slot)["release_id"] == first
    with pytest.raises(ValueError, match="another activation"):
        activate(tmp_path, slot, second, restored["operation_id"], restored["operation_id"])
    with pytest.raises(OSError):
        release_files.prepare(
            tmp_path, first, data, files, hashlib.sha256(data).hexdigest(), "a" * 64
        )
    assert release_files.pointer(tmp_path, slot) == restored


def test_only_one_competing_activation_can_replace_the_expected_target(tmp_path):
    slot = uuid.uuid4().hex
    first, _, _ = bundle(tmp_path, "First")
    other, _, _ = bundle(tmp_path, "Other")
    barrier = threading.Barrier(2)

    def race(key):
        barrier.wait(timeout=5)
        try:
            activate(tmp_path, slot, key)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [first, other]))
    assert sorted(results) == [False, True]


def test_corrupt_prepared_bundles_and_wrong_configuration_cannot_replace_active_release(tmp_path):
    slot = uuid.uuid4().hex
    first, _, _ = bundle(tmp_path, "First")
    second, _, _ = bundle(tmp_path, "Second")
    one = activate(tmp_path, slot, first)
    with pytest.raises(ValueError, match="configuration"):
        release_files.activate(
            tmp_path, slot, second, "b" * 64, one["operation_id"], uuid.uuid4().hex
        )
    (tmp_path / "bundles" / second / "artifact.zip").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="changed"):
        activate(tmp_path, slot, second, one["operation_id"])
    assert release_files.pointer(tmp_path, slot) == one


def test_serving_follows_atomic_pointer_and_rejects_missing_or_corrupt_releases(tmp_path):
    slot = uuid.uuid4().hex
    first, _, _ = bundle(tmp_path, "First")
    second, _, _ = bundle(tmp_path, "Second")
    server = HTTPServer(("127.0.0.1", 0), ReleaseHandler)
    server.root, server.public_port = tmp_path, 8032
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path="/"):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", path, headers={"Host": f"{slot}.localhost:8032"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        assert get()[0] == 404
        one = activate(tmp_path, slot, first)
        assert get() == (200, b"<h1>First</h1>")
        two = activate(tmp_path, slot, second, one["operation_id"])
        assert get() == (200, b"<h1>Second</h1>")
        activate(tmp_path, slot, first, two["operation_id"])
        assert get() == (200, b"<h1>First</h1>")
        assert get("/../targets/")[0] == 404
        (tmp_path / "bundles" / first / "artifact.zip").write_bytes(b"corrupt")
        assert get()[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_serving_content_types_ignore_host_mime_overrides(monkeypatch):
    import mimetypes

    from tempo.preview_server import content_type

    monkeypatch.setitem(mimetypes.types_map, ".xml", "application/x-host-specific")
    assert content_type("sitemap.xml") == "text/xml"
    assert content_type("app.js") == "text/javascript"
    assert content_type("worker.wasm") == "application/wasm"
