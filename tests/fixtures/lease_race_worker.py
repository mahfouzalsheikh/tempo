"""Independent database connection for PostgreSQL lease/checkpoint race tests."""

import asyncio
import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from tempo.persistence import PersistenceStore  # noqa: E402

print("ready", flush=True)
sys.stdin.readline()


async def main():
    store = PersistenceStore("memory")
    mode, run_id, worker = sys.argv[1:]
    if mode == "claim":
        result = await store.claim_run(int(run_id), worker)
        print(json.dumps({"claimed": bool(result)}), flush=True)
    else:
        for index in range(20):
            await store.checkpoint(
                int(run_id), "concurrent", {"worker": worker, "index": index},
                idempotency_key=f"{worker}:{index}",
                lease_token=os.environ["TEMPO_TEST_LEASE_TOKEN"],
            )
        print(json.dumps({"written": 20}), flush=True)


asyncio.run(main())
