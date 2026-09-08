import json
import uuid

from django import forms
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from tempo.acceptance import approve, checked_suite, configured_image, enqueue, identity
from tempo.acceptance_contract import digest, verify_report
from tempo.errors import CodexError, ConfigError

from .artifact_views import verified_candidate_artifact
from .intake_views import signed_in
from .models import BuildArtifact


def summary(artifact):
    suite = artifact.acceptance_suites.order_by("-id").first()
    if not suite:
        return {"status": "No reviewed browser checks"}
    latest = suite.attempts.order_by("-id").first()
    result = {"suite": suite, "attempt": latest, "status": "Ready to check", "results": []}
    try:
        checked_suite(suite)
    except (ValueError, KeyError, TypeError):
        result.update(status="Check plan unavailable", invalid=True)
        return result
    if latest:
        result["status"] = latest.status
        if latest.status in {"passed", "failed"}:
            try:
                verified_candidate_artifact(artifact)
                if latest.identity != identity(
                    artifact, suite, latest.image
                ) or latest.report_digest != digest(latest.report):
                    raise ValueError("Evidence identity changed")
                passed = verify_report(latest.report, checked_suite(suite), artifact.digest)
                if passed != (latest.status == "passed"):
                    raise ValueError("Evidence status changed")
                result["results"] = latest.report["results"]
                result["passed"] = passed
            except (ValueError, KeyError, TypeError, CodexError, ConfigError):
                result.update(status="Evidence unavailable", passed=False)
    return result


def check_form(criteria, instructions, data=None):
    form = forms.Form(data=data)
    for index, criterion in enumerate(criteria):
        form.fields[f"criterion_{index}"] = forms.CharField(
            label=f"{criterion['id']}: {criterion['outcome']}",
            help_text=criterion["verification"],
            max_length=8000,
            initial=instructions.get(criterion["id"], ""),
            widget=forms.Textarea(attrs={"rows": 5, "spellcheck": "false"}),
        )
    form.fields["reviewed"] = forms.BooleanField(
        label="I reviewed these checks against every expected outcome in the saved brief.",
    )
    return form


def checks(request, brief_id, run_id, artifact_id):
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
    try:
        verified_candidate_artifact(artifact)
    except (ValueError, KeyError, CodexError, ConfigError):
        from .intake_views import page

        response = page(
            request,
            "error",
            brief_id=brief_id,
            error="The saved build's evidence is unavailable. Acceptance checks are blocked.",
        )
        response.status_code = 409
        return response
    info = summary(artifact)
    suite = info.get("suite")
    criteria = artifact.run.product_snapshot["brief"]["criteria"]
    instructions = (
        {check["id"]: check["instructions"] for check in suite.specification["checks"]}
        if suite and not info.get("invalid")
        else {}
    )
    form = check_form(
        criteria,
        instructions,
        request.POST
        if request.method == "POST" and request.POST.get("action") == "approve"
        else None,
    )
    error, configured = "", True
    try:
        configured_image()
    except ValueError as exc:
        error, configured = str(exc), False
    if request.method == "POST":
        try:
            if request.POST.get("action") == "approve" and form.is_valid():
                approve(
                    artifact.pk,
                    {
                        criterion["id"]: form.cleaned_data[f"criterion_{index}"]
                        for index, criterion in enumerate(criteria)
                    },
                    int(request.POST.get("expected_suite_id", "0")),
                    request.user.pk,
                )
                return redirect(request.path)
            if request.POST.get("action") == "run":
                enqueue(
                    artifact.pk,
                    int(request.POST.get("suite_id", "0")),
                    request.POST.get("suite_digest", ""),
                    uuid.UUID(request.POST.get("request_key", "")),
                    request.user.pk,
                )
                return redirect(request.path)
            error = "Review the check instructions and required confirmation below."
        except (ValueError, KeyError, TypeError, CodexError, ConfigError) as exc:
            error = str(exc)
    return render(
        request,
        "acceptance.html",
        {
            "brief_id": brief_id,
            "artifact": artifact,
            "form": form,
            "info": info,
            "error": error,
            "configured": configured,
            "request_key": uuid.uuid4(),
            "expected_suite_id": suite.pk if suite else 0,
            "report": json.dumps(info["attempt"].report, indent=2) if info.get("attempt") else "",
        },
        status=409 if request.method == "POST" else 200,
    )
