"""Static staging adapter: immutable bundles and atomic compare-and-swap pointers."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from tempo.preview_files import MAX_BYTES, TOKEN, inventory, sync_directory

ADAPTER = "local-static-v1"


def identifier(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError("Invalid staging identifier")
    return value


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


def prepare(root, release_id, data, files, artifact_digest, configuration_digest):
    identifier(release_id)
    if not isinstance(configuration_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", configuration_digest
    ):
        raise ValueError("Invalid release configuration digest")
    if hashlib.sha256(data).hexdigest() != artifact_digest:
        raise ValueError("Staging artifact digest mismatch")
    entries = inventory(data, files)
    document = {
        "schema": 1,
        "adapter": ADAPTER,
        "release_id": release_id,
        "sha256": artifact_digest,
        "configuration_digest": configuration_digest,
        "files": entries,
    }
    bundles = Path(root) / "bundles"
    bundles.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = bundles / release_id
    temporary = Path(tempfile.mkdtemp(prefix=".bundle-", dir=bundles))
    try:
        with (temporary / "artifact.zip").open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        write_json(temporary / "metadata.json", document)
        sync_directory(temporary)
        # Existing releases are immutable, including on retry.
        os.rename(temporary, target)
        sync_directory(bundles)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return document


def pointer(root, slot):
    identifier(slot)
    try:
        value = json.loads((Path(root) / "targets" / f"{slot}.json").read_text())
    except FileNotFoundError:
        return None
    if set(value) != {"schema", "slot", "release_id", "configuration_digest", "operation_id"}:
        raise ValueError("Invalid staging pointer")
    if value["schema"] != 1 or value["slot"] != slot:
        raise ValueError("Staging pointer identity mismatch")
    identifier(value["release_id"])
    identifier(value["operation_id"])
    return value


def document(root, slot):
    current = pointer(root, slot)
    if not current:
        raise ValueError("No release is active for this target")
    value = json.loads(
        (Path(root) / "bundles" / current["release_id"] / "metadata.json").read_text()
    )
    if (
        value["schema"] != 1
        or value["adapter"] != ADAPTER
        or value["release_id"] != current["release_id"]
        or value["configuration_digest"] != current["configuration_digest"]
    ):
        raise ValueError("Staging bundle identity mismatch")
    return value | {"pointer": current}


def activate(root, slot, release_id, configuration_digest, expected_operation, operation_id):
    """Adapter primitive. The future release coordinator must authorize every invocation."""
    for value in (slot, release_id, operation_id):
        identifier(value)
    root = Path(root)
    locks, targets = root / "locks", root / "targets"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    targets.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locks / f"{slot}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = pointer(root, slot)
        desired = {
            "schema": 1,
            "slot": slot,
            "release_id": release_id,
            "configuration_digest": configuration_digest,
            "operation_id": operation_id,
        }
        if current == desired:
            return current  # Reconcile a lost response without repeating publication.
        if current and current["operation_id"] == operation_id:
            raise ValueError("This operation ID belongs to another activation")
        if (current["operation_id"] if current else None) != expected_operation:
            raise ValueError("The staging target changed; refuse a stale activation")
        bundle = root / "bundles" / release_id
        saved = json.loads((bundle / "metadata.json").read_text())
        if (
            saved["schema"] != 1
            or saved["adapter"] != ADAPTER
            or saved["release_id"] != release_id
            or saved["configuration_digest"] != configuration_digest
        ):
            raise ValueError("Release configuration does not match prepared bundle")
        with (bundle / "artifact.zip").open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != saved["sha256"]:
            raise ValueError("Prepared bundle changed")
        inventory(data, list(saved["files"].values()))
        temporary = targets / f".pointer-{uuid.uuid4().hex}"
        try:
            write_json(temporary, desired)
            os.replace(temporary, targets / f"{slot}.json")
            sync_directory(targets)
        finally:
            temporary.unlink(missing_ok=True)
        return desired
