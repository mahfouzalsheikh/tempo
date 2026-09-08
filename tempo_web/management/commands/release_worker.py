import signal
import threading
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tempo import rollback_rehearsal


class Command(BaseCommand):
    help = "Run durable local staging rollback rehearsals without coding or Docker credentials."

    def handle(self, **options):
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            close_old_connections()
            Path("/tmp/release-heartbeat").touch()
            rollback_rehearsal.recover()
            if stop.is_set():
                break
            attempt = rollback_rehearsal.claim()
            if attempt:
                rollback_rehearsal.process(attempt)
            else:
                stop.wait(2)
