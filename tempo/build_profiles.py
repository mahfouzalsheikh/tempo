"""Versioned, host-owned build recipes; never replace a published recipe in place."""

from copy import deepcopy

from .config import ValidationCheckConfig, ValidationConfig
from .errors import CodexError
from .intake import IntakeConflict

MINI_APP = {
    "id": "react-mini-app-v1",
    "name": "React mini-app",
    "directory": "mini-app",
    "output": "mini-app/dist",
    "prepare": (
        "set -e\ncd mini-app\n"
        "ONNXRUNTIME_NODE_INSTALL=skip npm ci --no-audit --no-fund\n"
        "npm run prepare:assets"
    ),
    "timeout_ms": 1_800_000,
    "checks": [
        {
            "id": "release-mini-tests",
            "name": "Mini-app unit tests",
            "command": "cd mini-app && npm test",
        },
        {
            "id": "release-mini-build",
            "name": "Mini-app production build and distribution checks",
            "command": "cd mini-app && npm run build && npm run validate:dist",
        },
    ],
}


def build_profile(identity):
    if not identity:
        return None
    if identity != MINI_APP["id"]:
        raise IntakeConflict("Select a supported build target.")
    return deepcopy(MINI_APP)


def checked_profile(value):
    if not isinstance(value, dict) or value != build_profile(value.get("id")):
        raise IntakeConflict("The saved build recipe is invalid.")
    return deepcopy(value)


def with_build_checks(config, profile):
    result = config.model_copy(deep=True)
    extra = [ValidationCheckConfig.model_validate(check) for check in profile["checks"]]
    if set(check.id for check in extra) & set(
        check.id for check in result.validation.required_checks
    ):
        raise IntakeConflict("Project check IDs conflict with the selected build target.")
    result.validation.required_checks.extend(extra)
    try:
        result.validation = ValidationConfig.model_validate(result.validation.model_dump())
    except ValueError as exc:
        raise IntakeConflict(
            "The combined build and project checks exceed supported limits."
        ) from exc
    return result


async def prepare_build(profile, path, on_event):
    """Install locked dependencies/assets with public network access and no model/tracker grants."""
    import asyncio

    from .credentials import CONTROL_PLANE_SECRETS, process_environment
    from .workload import EXECUTION_SECRETS, start_workload, stop_workload

    await on_event({"event": "build_preparation_started", "profile": profile["id"]})
    environment = process_environment(forbidden=CONTROL_PLANE_SECRETS | EXECUTION_SECRETS)
    process = await start_workload(
        profile["prepare"],
        path,
        environment,
        kind="build-preparation",
        timeout_ms=profile["timeout_ms"],
        stdin=None,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = b""
    timed_out = False
    try:
        async with asyncio.timeout(profile["timeout_ms"] / 1000 + 5):
            while chunk := await process.stdout.read(8192):
                output = (output + chunk)[-40_000:]
            await process.wait()
    except TimeoutError:
        timed_out = True
    finally:
        await stop_workload(process)
    await on_event(
        {
            "event": "build_preparation_completed",
            "exit_code": process.returncode,
            "output": output.decode(errors="replace"),
        }
    )
    if timed_out:
        raise CodexError(
            "Build dependency or asset preparation timed out.", category="product_checks_failed"
        )
    if process.returncode:
        raise CodexError(
            "Build dependency or asset preparation failed; inspect the build output.",
            category="product_checks_failed",
        )
