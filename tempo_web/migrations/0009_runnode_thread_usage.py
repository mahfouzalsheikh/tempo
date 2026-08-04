from django.db import migrations, models


def copy_existing_usage(apps, schema_editor):
    RunNode = apps.get_model("tempo_web", "RunNode")
    RunNode.objects.all().update(
        thread_input_tokens=models.F("input_tokens"),
        thread_output_tokens=models.F("output_tokens"),
        thread_total_tokens=models.F("total_tokens"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("tempo_web", "0008_merge_workflow_and_agent_session"),
    ]

    operations = [
        migrations.AddField(
            model_name="runnode",
            name="thread_input_tokens",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="runnode",
            name="thread_output_tokens",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="runnode",
            name="thread_total_tokens",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.RunPython(copy_existing_usage, migrations.RunPython.noop),
    ]
