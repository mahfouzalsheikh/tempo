"""Installation-owned validation image permissions, separate from frozen run inputs."""

import json
import os
import re

from .errors import ConfigError

IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
MAX_RETAINED_IMAGES = 256


def allowed_images(default):
    try:
        raw = os.getenv("TEMPO_VALIDATION_RETAINED_IMAGES", "[]")
        if len(raw) > 20000:
            raise ValueError("oversized catalog")
        images = json.loads(raw)
        if (not isinstance(images, list) or len(images) > MAX_RETAINED_IMAGES
                or any(not isinstance(value, str) or not IMAGE_ID.fullmatch(value)
                       for value in images)):
            raise ValueError("invalid catalog")
        return frozenset([default, *images])
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            "Retained validation images must be a bounded JSON list of image IDs."
        ) from exc


def retained_images():
    """Read verified history; legacy/invalid contracts never authorize another image."""
    from django.db import connection

    from tempo_web.models import AgentRun, WorkflowVersion

    from .product_execution import restore_product
    from .run_snapshot import restore_snapshot

    images, skipped = set(), []
    if "tempo_web_agentrun" not in connection.introspection.table_names():
        return {"images": [], "skipped": []}  # First deployment, before migrations.

    def collect(snapshot, digest, kind, identity, run=None):
        try:
            _, config = restore_snapshot(snapshot, digest)
            if run and (run.execution_plan_id or run.product_snapshot):
                restore_product(run)
            environment = snapshot["execution"]["environment"]
            if environment["TEMPO_VALIDATION_BACKEND"] != "docker":
                return
            image = config.validation.runner_image or environment["TEMPO_VALIDATION_IMAGE"]
            if image is None:
                return
            if not IMAGE_ID.fullmatch(image):
                raise ConfigError("mutable image", category="image_not_immutable")
            images.add(image)
        except ConfigError as exc:
            skipped.append({"kind": kind, "id": identity, "reason": exc.category})

    for version in WorkflowVersion.objects.exclude(execution_snapshot={}).iterator():
        collect(version.execution_snapshot, version.checksum, "workflow", version.pk)
    for run in AgentRun.objects.exclude(execution_snapshot={}).iterator():
        collect(run.execution_snapshot, run.snapshot_digest, "run", run.pk, run)
    if len(images) > MAX_RETAINED_IMAGES:
        raise ConfigError("Too many retained validation images; review image retention first.")
    return {"images": sorted(images), "skipped": skipped}


if __name__ == "__main__":
    import sys

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
    django.setup()
    catalog = retained_images()
    for skipped in catalog["skipped"]:
        print(f"Retained-image catalog skipped {skipped['kind']} {skipped['id']}: "
              f"{skipped['reason']}", file=sys.stderr)
    print(json.dumps(catalog["images"], separators=(",", ":")))
