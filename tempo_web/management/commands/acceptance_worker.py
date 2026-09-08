import signal
import threading
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tempo import preview_health
from tempo.acceptance import claim, process, recover


class Command(BaseCommand):
    help = "Run durable browser acceptance and preview health jobs without model credentials."

    def handle(self, **options):
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            close_old_connections()
            Path("/tmp/acceptance-heartbeat").touch()
            recover()
            preview_health.recover()
            attempt = claim()
            if attempt:
                process(attempt)
            if stop.is_set():
                break
            Path("/tmp/acceptance-heartbeat").touch()
            health_attempt = preview_health.claim()
            if health_attempt:
                preview_health.process(health_attempt)
            if not attempt and not health_attempt:
                stop.wait(2)
