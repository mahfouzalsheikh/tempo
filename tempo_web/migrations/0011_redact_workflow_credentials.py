"""Remove previously resolved tracker credentials from historical snapshots.

Snapshots are audit metadata, not the live credential source. References are retained;
plaintext cannot safely be reconstructed as a reference. Rotation is still required.
"""

import re

from django.db import migrations


def redact_workflow_credentials(apps, schema_editor):
    workflows = apps.get_model("tempo_web", "WorkflowVersion")
    database = schema_editor.connection.alias
    for row in workflows.objects.using(database).only("pk", "config").iterator(chunk_size=200):
        if not isinstance(row.config, dict):
            continue
        tracker = row.config.get("tracker")
        provider = tracker.get("provider") if isinstance(tracker, dict) else None
        if not isinstance(provider, dict):
            continue
        changed = False
        for key in ("token", "api_key", "review_token"):
            value = provider.get(key)
            if value is None or value == "" or (
                isinstance(value, str) and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", value)
            ):
                continue
            if value != "[REDACTED]":
                provider[key] = "[REDACTED]"
                changed = True
        if changed:
            workflows.objects.using(database).filter(pk=row.pk).update(config=row.config)


class Migration(migrations.Migration):
    dependencies = [("tempo_web", "0010_validation_policy_evidence")]
    operations = [migrations.RunPython(redact_workflow_credentials, migrations.RunPython.noop)]
