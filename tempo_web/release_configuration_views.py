from django import forms
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from tempo import release_configuration
from tempo.errors import CodexError, ConfigError

from .artifact_views import verified_candidate_artifact
from .intake_views import signed_in
from .models import BuildArtifact, DeploymentTarget


class ConfigurationForm(forms.Form):
    target_key = forms.RegexField(
        regex=r"^[a-z][a-z0-9-]{0,47}$",
        max_length=48,
        label="Staging target name",
        help_text="Reuse this name for future builds at the same address, e.g. mini-app-staging.",
        widget=forms.TextInput(attrs={"list": "staging-targets", "autocomplete": "off"}),
    )
    reviewed = forms.BooleanField(
        label="I reviewed this destination and confirm that this build needs no server, "
        "runtime variables, secrets, or database migrations.",
    )


def configure(request, brief_id, run_id, artifact_id):
    if response := signed_in(request):
        return response
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    artifact = get_object_or_404(
        BuildArtifact,
        pk=artifact_id,
        run_id=run_id,
        run__execution_plan__brief_revision__brief_id=brief_id,
    )
    info = release_configuration.summary(artifact)
    saved = info["configuration"]
    form = ConfigurationForm(
        request.POST if request.method == "POST" else None,
        initial={"target_key": saved.target.key if saved else "mini-app-staging"},
    )
    error, enabled, port = "", True, None
    try:
        verified_candidate_artifact(artifact)
        _root, port = release_configuration.settings()
        if request.method == "POST" and form.is_valid():
            release_configuration.approve(
                artifact.pk,
                form.cleaned_data["target_key"],
                int(request.POST.get("expected_configuration_id", "0")),
                request.user.pk,
            )
            return redirect(request.path)
    except (ValueError, KeyError, TypeError, OSError, CodexError, ConfigError) as exc:
        error, enabled = str(exc), False
    return render(
        request,
        "release_configuration.html",
        {
            "artifact": artifact,
            "brief_id": brief_id,
            "info": info,
            "form": form,
            "error": error,
            "enabled": enabled,
            "port": port,
            "expected_configuration_id": saved.pk if saved else 0,
            "targets": DeploymentTarget.objects.filter(
                project_id=artifact.run.execution_plan.brief_revision.brief.project_id
            ).order_by("key"),
        },
        status=409 if request.method == "POST" else 200,
    )
