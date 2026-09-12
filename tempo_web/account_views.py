import json

from asgiref.sync import async_to_sync
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie

from tempo import agent_accounts as accounts
from tempo.errors import ConfigError
from tempo.run_snapshot import snapshot_digest
from tempo.runtime import get_orchestrator

from .intake_views import signed_in
from .models import AgentAccount, Organization, Project


class ConnectionForm(forms.Form):
    organization = forms.ModelChoiceField(queryset=Organization.objects.all())
    label = forms.CharField(label="Connection name", max_length=100)
    auth_mode = forms.ChoiceField(
        label="Login source",
        choices=AgentAccount._meta.get_field(
            "auth_mode",
        ).choices,
    )


def controllers():
    service = get_orchestrator()
    return [
        c for c in getattr(service, "orchestrators", [service]) if getattr(c, "persistence", None)
    ]


def assign(user, data):
    fields = {"action", "project_id", "profile", "account_id", "revision", "configuration_digest"}
    if (
        set(data) != fields
        or type(data["project_id"]) is not int
        or type(data["revision"]) is not int
    ):
        raise ValueError("Invalid assignment request.")
    for controller in controllers():
        if controller.persistence.project_id == data["project_id"]:
            async_to_sync(controller.assign_account)(
                user,
                data["profile"],
                data["account_id"],
                data["revision"],
                data["configuration_digest"],
            )
            return
    raise ValueError("The project workflow is not loaded.")


def teams():
    rows = []
    for controller in controllers():
        store = controller.persistence
        project = Project.objects.get(pk=store.project_id)
        available = AgentAccount.objects.filter(
            grants__project=project,
            grants__active=True,
            disabled=False,
            check_status="stored",
            organization=project.organization,
        )
        for name, profile in controller.store.current()[1].agents.items():
            binding = profile.settings.get("account")
            current = AgentAccount.objects.filter(pk=binding["id"]).first() if binding else None
            rows.append(
                {
                    "project": project,
                    "profile": name,
                    "current": current,
                    "choices": list(available),
                    "digest": snapshot_digest(store.execution_snapshot),
                }
            )
    return rows


def apply(user, data):
    action = data.get("action")
    fields = {
        "create": {"action", "organization_id", "label", "auth_mode"},
        "settings": {"action", "account_id", "revision", "disabled", "project_ids"},
        "check": {"action", "account_id", "revision"},
    }
    if action not in fields or set(data) != fields[action]:
        raise ValueError("Unsupported account request fields.")
    if action == "create":
        return accounts.create(
            user,
            Organization.objects.get(pk=data["organization_id"]),
            data["label"],
            data["auth_mode"],
        )
    if type(data["revision"]) is not int:
        raise ValueError("Provide the connection revision.")
    if action == "check":
        return accounts.check(user, data["account_id"], data["revision"])
    return accounts.update(
        user,
        data["account_id"],
        data["revision"],
        disabled=data["disabled"],
        project_ids=data["project_ids"],
    )


@ensure_csrf_cookie
def index(request):
    if response := signed_in(request):
        return response
    if not request.user.is_staff:
        return HttpResponseForbidden("Account administration requires a staff operator.")
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    error = ""
    form = ConnectionForm()
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "create":
                form = ConnectionForm(request.POST)
                if not form.is_valid():
                    raise ValueError("Check the connection details below.")
                accounts.create(request.user, **form.cleaned_data)
            elif action == "assign":
                selection = request.POST.get("selection", "")
                account_id, revision = selection.split(":") if selection else ("", "0")
                assign(
                    request.user,
                    {
                        "action": "assign",
                        "project_id": int(request.POST["project_id"]),
                        "profile": request.POST["profile"],
                        "account_id": account_id,
                        "revision": int(revision),
                        "configuration_digest": request.POST["configuration_digest"],
                    },
                )
            elif action == "check":
                accounts.check(
                    request.user, request.POST["account_id"], int(request.POST["revision"])
                )
            elif action == "settings":
                accounts.update(
                    request.user,
                    request.POST["account_id"],
                    int(request.POST["revision"]),
                    disabled=request.POST.get("disabled") == "on",
                    project_ids=[int(pk) for pk in request.POST.getlist("projects")],
                )
            else:
                raise ValueError("Unsupported action.")
            return redirect("agent_accounts")
        except (
            ValueError,
            KeyError,
            IntegrityError,
            ValidationError,
            ConfigError,
            AgentAccount.DoesNotExist,
        ):
            error = "The connection could not be updated. Check the form and reload if it changed."
    cards = []
    for account in AgentAccount.objects.select_related("organization").prefetch_related("grants"):
        saved = accounts.public_record(account)
        cards.append(
            {
                "account": account,
                "saved": saved,
                "projects": Project.objects.filter(organization=account.organization, active=True),
                "events": account.events.select_related("actor")[:5],
            }
        )
    return render(
        request,
        "agent_accounts.html",
        {"cards": cards, "form": form, "error": error, "teams": teams()},
        status=409 if error else 200,
    )


def api(request):
    request.tempo_private_response = True
    if not request.user.is_authenticated:
        return JsonResponse({"error": "authentication_required"}, status=401)
    try:
        accounts.administrator(request.user)
    except PermissionDenied:
        return JsonResponse({"error": "staff_required"}, status=403)
    if request.method == "GET":
        return JsonResponse(
            {"accounts": [accounts.public_record(a) for a in AgentAccount.objects.all()]}
        )
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    try:
        if len(request.body) > 16000:
            raise ValueError
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError
        if data.get("action") == "assign":
            assign(request.user, data)
            return JsonResponse({"status": "assigned"})
        account = apply(request.user, data)
    except (
        ValueError,
        TypeError,
        KeyError,
        IntegrityError,
        Organization.DoesNotExist,
        AgentAccount.DoesNotExist,
        ValidationError,
        ConfigError,
    ):
        return JsonResponse({"error": "invalid_or_stale_account_request"}, status=409)
    return JsonResponse({"account": accounts.public_record(account)})
