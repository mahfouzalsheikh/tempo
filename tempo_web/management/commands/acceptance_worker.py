import signal
import threading
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tempo.acceptance import claim, process, recover


class Command(BaseCommand):
    help = "Run durable browser acceptance jobs without model or tracker credentials."

    def handle(self, **options):
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            close_old_connections()
            Path("/tmp/acceptance-heartbeat").touch()
            recover()
            attempt = claim()
            if attempt:
                process(attempt)
            else:
                stop.wait(2)
