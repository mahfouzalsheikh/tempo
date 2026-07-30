from django.db import migrations, models


def copy_existing_usage(apps, schema_editor):
    AgentSession = apps.get_model("tempo_web", "AgentSession")
    for session in AgentSession.objects.all().iterator():
        session.thread_input_tokens = session.input_tokens
        session.thread_output_tokens = session.output_tokens
        session.thread_total_tokens = session.total_tokens
        session.save(
            update_fields=[
                "thread_input_tokens",
                "thread_output_tokens",
                "thread_total_tokens",
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("tempo_web", "0003_scope_legacy_runtime_rows"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="agent_role",
            field=models.CharField(default="implementation", max_length=32),
        ),
        migrations.AddField(
            model_name="agentsession",
            name="thread_input_tokens",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="agentsession",
            name="thread_output_tokens",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="agentsession",
            name="thread_total_tokens",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.RunPython(copy_existing_usage, migrations.RunPython.noop),
    ]
