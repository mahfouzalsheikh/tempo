from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from .logging import configure_logging
from .orchestrator import Orchestrator
from .runtime import set_orchestrator


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tempo",
        description="Run the Python Tempo orchestrator and Django observability server.",
    )
    result.add_argument(
        "workflow",
        nargs="?",
        default=os.getenv("TEMPO_WORKFLOW_PATH", "./WORKFLOW.md"),
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8000)
    result.add_argument("--no-http", action="store_true", help="Run only the orchestrator")
    return result


async def run(args: argparse.Namespace) -> None:
    path = Path(args.workflow)
    if not path.is_file():  # noqa: ASYNC240 - one startup metadata check
        raise SystemExit(f"Workflow file does not exist: {path}")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
    configure_logging()
    orchestrator = Orchestrator(str(path))
    await orchestrator.start()
    set_orchestrator(orchestrator)
    try:
        if args.no_http:
            await asyncio.Event().wait()
        else:
            server = uvicorn.Server(
                uvicorn.Config(
                    "tempo_web.asgi:application",
                    host=args.host,
                    port=args.port,
                    log_config=None,
                    access_log=False,
                )
            )
            await server.serve()
    finally:
        set_orchestrator(None)
        await orchestrator.stop()


def main() -> None:
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
