"""Bounded static bundles and identity-bound release evidence, owned by the controller."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from pathlib import Path

from .errors import CodexError
from .run_snapshot import snapshot_digest

MAX_FILES = 10_000
MAX_BYTES = 128 * 1024 * 1024


def artifact_error(message):
    return CodexError(message, category="product_checks_failed")


def package_directory(workspace: Path, relative: str):
    """Read beneath directory FDs, rejecting links and special files; never follow output paths."""
    output = io.BytesIO()
    inventory = []
    total = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(workspace, flags)
    try:
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in {".", ".."} for p in parts):
            raise artifact_error("Invalid build output directory.")
        for part in parts:
            child = os.open(part, flags, dir_fd=root)
            os.close(root)
            root = child
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:

            def visit(directory, prefix=""):
                nonlocal total
                for name in sorted(os.listdir(directory)):
                    if name in {".git", ".env"} or "\\" in name or any(ord(c) < 32 for c in name):
                        raise artifact_error("Build output contains an unsupported file name.")
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    member = prefix + name
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, flags, dir_fd=directory)
                        try:
                            visit(child, member + "/")
                        finally:
                            os.close(child)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise artifact_error("Build output must contain regular, unlinked files.")
                    if len(inventory) >= MAX_FILES or total + info.st_size > MAX_BYTES:
                        raise artifact_error("Build output exceeds the supported artifact limits.")
                    fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                            raise artifact_error("Build output changed during capture.")
                        data = stream.read(MAX_BYTES - total + 1)
                    total += len(data)
                    if total > MAX_BYTES:
                        raise artifact_error("Build output exceeds the supported artifact limits.")
                    item = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
                    item.external_attr = (stat.S_IFREG | 0o644) << 16
                    item.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(item, data)
                    inventory.append(
                        {
                            "path": member,
                            "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    )

            visit(root)
    except (OSError, ValueError, RecursionError) as exc:
        raise artifact_error("Build output cannot be captured safely.") from exc
    finally:
        os.close(root)
    if not any(item["path"] == "index.html" and item["size"] for item in inventory):
        raise artifact_error("The static build has no nonempty index.html.")
    data = output.getvalue()
    if len(data) > MAX_BYTES:
        raise artifact_error("The archive exceeds the supported artifact limit.")
    return {
        "data": data,
        "digest": hashlib.sha256(data).hexdigest(),
        "files": inventory,
        "size": len(data),
    }


def artifact_manifest(context, candidate, bundle):
    source = context["source_snapshot"]
    environment = source["execution"]["environment"]
    return {
        "schema": 1,
        "kind": "static-web-build",
        "build_profile": context["build_profile"],
        "source_sha": candidate["source_sha"],
        "snapshot_digest": candidate["snapshot_digest"],
        "brief_digest": candidate["brief_digest"],
        "plan_digest": candidate["plan_digest"],
        "plan_id": candidate["plan_id"],
        "validation_record_id": candidate["validation_record_id"],
        "policy_digest": candidate["policy_digest"],
        "required_check_ids": candidate["required_check_ids"],
        "workspace_fingerprint": candidate["workspace_fingerprint"],
        "execution": {
            "runtime_backend": environment["TEMPO_RUNTIME_BACKEND"],
            "runtime_image": environment["TEMPO_RUNTIME_IMAGE"]
            or environment["TEMPO_VALIDATION_IMAGE"],
            "validation_image": source["config"]["validation"]["runner_image"]
            or environment["TEMPO_VALIDATION_IMAGE"],
            "validation_status": "passed",
        },
        "archive": {"sha256": bundle["digest"], "size": bundle["size"], "format": "zip"},
        "files": bundle["files"],
        "acceptance": [
            {"id": criterion["id"], "status": "unverified"}
            for criterion in context["brief"]["criteria"]
        ],
        "readiness": {
            "deployment_ready": False,
            "remaining": ["independent_acceptance", "preview", "promotion_and_rollback"],
        },
    }


def verify_artifact(artifact):
    data = bytes(artifact.data)
    if (
        not data
        or len(data) > MAX_BYTES
        or len(data) != artifact.size
        or hashlib.sha256(data).hexdigest() != artifact.digest
        or snapshot_digest(artifact.manifest) != artifact.manifest_digest
        or artifact.manifest.get("archive")
        != {
            "sha256": artifact.digest,
            "size": artifact.size,
            "format": "zip",
        }
    ):
        raise artifact_error("Saved build artifact failed its integrity check.")
    return data


def clear_output(workspace, relative):
    import shutil

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(workspace, flags)
    try:
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in {".", ".."} for p in parts):
            raise artifact_error("Invalid build output directory.")
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=root)
            os.close(root)
            root = child
        try:
            os.stat(parts[-1], dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            return
        shutil.rmtree(parts[-1], dir_fd=root)
    except OSError as exc:
        raise artifact_error("Existing build output cannot be cleared safely.") from exc
    finally:
        os.close(root)
