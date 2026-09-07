"""Best-effort cleanup for child processes launched in a dedicated POSIX session."""

import asyncio
import contextlib
import os
import signal


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        pass
    finally:
        # The leader can exit while descendants still hold pipes or keep running.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
