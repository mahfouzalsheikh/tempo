from django.db import migrations


def scope_legacy_rows(apps, schema_editor):
    Organization = apps.get_model("tempo_web", "Organization")
    Project = apps.get_model("tempo_web", "Project")
    TrackedIssue = apps.get_model("tempo_web", "TrackedIssue")
    AgentRun = apps.get_model("tempo_web", "AgentRun")

    organization, _ = Organization.objects.get_or_create(
        slug="default",
        defaults={"name": "Default"},
    )
    project, _ = Project.objects.get_or_create(
        organization=organization,
        slug="default",
        defaults={"name": "Default project"},
    )
    TrackedIssue.objects.filter(project__isnull=True).update(project=project)
    AgentRun.objects.filter(project__isnull=True).update(project=project)


class Migration(migrations.Migration):
    dependencies = [
        (
            "tempo_web",
            "0002_approvalrequest_credentialreference_environment_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(scope_legacy_rows, migrations.RunPython.noop),
    ]
