"""Credential-free, file-only contract shared by the operator and preview server."""

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 10_000
TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def inventory(data, files):
    """Validate every member before making a saved ZIP available to browsers."""
    if not 0 < len(data) <= MAX_BYTES or not isinstance(files, list):
        raise ValueError("Invalid preview archive")
    expected = {item["path"]: item for item in files}
    if len(expected) != len(files) or not 0 < len(files) <= MAX_FILES:
        raise ValueError("Invalid preview inventory")
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) != len(files) or {m.filename for m in members} != set(expected):
            raise ValueError("Preview inventory does not match archive")
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or str(path) != name
                or any(p in {".", "..", ".git"} or p.startswith(".env") for p in path.parts)
                or any(ord(c) < 32 or c in "\\:" for c in name)
                or member.is_dir()
                or member.flag_bits & 1
                or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or (member.external_attr >> 16) & 0o170000 not in {0, 0o100000}
            ):
                raise ValueError("Unsafe preview archive member")
            total += member.file_size
            if total > MAX_BYTES or member.file_size != expected[name]["size"]:
                raise ValueError("Preview file size mismatch")
            content = archive.read(member)
            if hashlib.sha256(content).hexdigest() != expected[name]["sha256"]:
                raise ValueError("Preview file digest mismatch")
    if expected.get("index.html", {}).get("size", 0) <= 0:
        raise ValueError("Preview requires index.html")
    return expected


def metadata(root, token):
    if not TOKEN.fullmatch(token):
        raise ValueError("Invalid preview identifier")
    document = json.loads((Path(root) / token / "metadata.json").read_text())
    if document["schema"] != 1 or document["expires_at"] <= time.time():
        raise ValueError("Preview expired")
    return document


def sync_directory(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(root, token, data, files, digest, expires_at):
    if not TOKEN.fullmatch(token) or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Invalid preview identity")
    entries = inventory(data, files)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Expiration is enforced by the reader even if no further launches occur.
    # Reclaim expired files when an operator next launches a preview.
    for directory in root.iterdir():
        if TOKEN.fullmatch(directory.name) and directory.is_dir():
            try:
                metadata(root, directory.name)
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    remove(root, directory.name)
                except FileNotFoundError:
                    pass  # Another launch may already have reclaimed this expired preview.
        elif directory.name.startswith(".preview-") and directory.is_dir():
            try:
                if directory.stat().st_mtime < time.time() - 86400:
                    shutil.rmtree(directory)
            except FileNotFoundError:
                pass
    staging = Path(tempfile.mkdtemp(prefix=".preview-", dir=root))
    try:
        document = {"schema": 1, "expires_at": expires_at, "sha256": digest, "files": entries}
        for name, content in (
            ("artifact.zip", data),
            ("metadata.json", json.dumps(document, sort_keys=True).encode()),
        ):
            with (staging / name).open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        sync_directory(staging)
        os.rename(staging, root / token)
        sync_directory(root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def remove(root, token):
    if not TOKEN.fullmatch(token):
        raise ValueError("Invalid preview identifier")
    directory = Path(root) / token
    # Remove the serving authorization before doing the larger file deletion.
    (directory / "metadata.json").unlink(missing_ok=True)
    if directory.exists():
        sync_directory(directory)
        shutil.rmtree(directory)
        sync_directory(root)


def available(root, token, digest):
    try:
        return (
            metadata(root, token)["sha256"] == digest
            and (Path(root) / token / "artifact.zip").is_file()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
