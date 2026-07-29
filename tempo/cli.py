from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from .control_plane import ControlPlane
from .logging import configure_logging
from .runtime import set_orchestrator


def prepare_database() -> None:
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    call_command("migrate", interactive=False, verbosity=1)
    username = os.getenv("TEMPO_ADMIN_USERNAME", "").strip()
    password = os.getenv("TEMPO_ADMIN_PASSWORD", "")
    email = os.getenv("TEMPO_ADMIN_EMAIL", "").strip()
    if not username or not password:
        return
    user_model = get_user_model()
    if not user_model.objects.filter(username=username).exists():
        user_model.objects.create_superuser(username=username, email=email, password=password)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tempo",
        description="Run the Python Tempo orchestrator and Django observability server.",
    )
    result.add_argument(
        "workflow",
        nargs="*",
        default=[os.getenv("TEMPO_WORKFLOW_PATH", "./WORKFLOW.md")],
        help="One or more WORKFLOW.md paths (default: ./WORKFLOW.md)",
    )
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8000)
    result.add_argument("--no-http", action="store_true", help="Run only the orchestrator")
    return result


async def run(args: argparse.Namespace) -> None:
    paths = [Path(value) for value in args.workflow]
    for path in paths:
        if not path.is_file():  # noqa: ASYNC240 - startup metadata checks
            raise SystemExit(f"Workflow file does not exist: {path}")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
    configure_logging()
    await asyncio.to_thread(prepare_database)
    control_plane = ControlPlane([str(path) for path in paths])
    await control_plane.start()
    set_orchestrator(control_plane)
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
        await control_plane.stop()


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
    import django

    django.setup()
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
