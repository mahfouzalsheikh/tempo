"""Read-only deployment checks for stored execution snapshots; no jobs are dispatched."""

import asyncio
import copy
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from tempo.errors import ConfigError  # noqa: E402
from tempo.run_snapshot import (  # noqa: E402
    execution_setting,
    execution_settings,
    restore_snapshot,
    snapshot_digest,
)
from tempo_web.models import AgentRun, WorkflowVersion  # noqa: E402

version = WorkflowVersion.objects.exclude(execution_snapshot={}).order_by("-id").first()
assert version, "No complete workflow snapshot was recorded at startup"
source, config = restore_snapshot(version.execution_snapshot, version.checksum)
assert version.checksum == snapshot_digest(version.execution_snapshot)
for run in AgentRun.objects.exclude(execution_snapshot={}):
    restore_snapshot(run.execution_snapshot, run.snapshot_digest)
legacy = AgentRun.objects.filter(execution_snapshot={}).count()


async def main():
    original = copy.deepcopy(version.execution_snapshot)
    alternate = copy.deepcopy(original)
    alternate["execution"]["environment"]["TEMPO_RUNTIME_IMAGE"] = "probe-only-alternate"

    async def observe(snapshot):
        with execution_settings(snapshot["execution"]):
            await asyncio.sleep(0)
            return execution_setting("TEMPO_RUNTIME_IMAGE")

    first, second = await asyncio.gather(observe(original), observe(alternate))
    assert first == original["execution"]["environment"]["TEMPO_RUNTIME_IMAGE"]
    assert second == "probe-only-alternate"
    alternate["prompt_template"] = "invalidated probe"
    try:
        restore_snapshot(alternate, version.checksum)
    except ConfigError as error:
        assert error.category == "snapshot_invalid"
    else:
        raise AssertionError("Changed snapshot was accepted")


asyncio.run(main())
print("Stored snapshots, isolated defaults, and tamper rejection verified; "
      f"{legacy} legacy runs retained.")
