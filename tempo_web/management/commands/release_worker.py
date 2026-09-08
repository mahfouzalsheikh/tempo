import signal
import threading
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tempo import releases, rollback_rehearsal


class Command(BaseCommand):
    help = "Publish local staging releases and run rollback rehearsals without coding credentials."

    def handle(self, **options):
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            close_old_connections()
            Path("/tmp/release-heartbeat").touch()
            rollback_rehearsal.recover()
            releases.recover()
            if stop.is_set():
                break
            publication = releases.claim()
            if publication:
                releases.process(publication)
            if stop.is_set():
                break
            Path("/tmp/release-heartbeat").touch()
            attempt = rollback_rehearsal.claim()
            if attempt:
                rollback_rehearsal.process(attempt)
            if not attempt and not publication:
                stop.wait(2)
