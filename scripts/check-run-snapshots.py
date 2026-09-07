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

# Query the additive migration columns, including on installations with no restarts yet.
list(AgentRun.objects.values_list("restarted_from_id", "fresh_workspace_key"))
for restarted in AgentRun.objects.exclude(restarted_from_id=None).select_related("restarted_from"):
    assert restarted.fresh_workspace_key.startswith("restart-")
    assert restarted.restarted_from.phase == "SupersededByRestart"
    assert restarted.restarted_from.status == "cancelled"
    assert restarted.restarted_from.idempotency_key is None
    assert not restarted.restarted_from.approvals.filter(status="pending").exists()

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
print("Fresh restart schema and successor lineage verified without dispatching work.")
