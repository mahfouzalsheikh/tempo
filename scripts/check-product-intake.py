"""Verify intake contracts and persisted revisions without creating production work."""
import os
import urllib.error
import urllib.request

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from tempo.contracts.intake import Brief, starter_plan  # noqa: E402
from tempo.intake import checked_brief, checked_plan  # noqa: E402
from tempo_web.models import BriefRevision, ExecutionPlan, ProductBrief  # noqa: E402

brief = Brief.model_validate({
    "title": "Intake probe", "goal": "Check contracts", "users": "Operators",
    "scope": "Planning", "target": "Local preview", "criteria": [
        {"id": "AC-1", "outcome": "A plan covers the brief",
         "verification": "Validate its task references and dependency order"},
    ],
})
plan = starter_plan(brief)
assert plan.validate_against(brief) == [["design"], ["build-1"], ["integrate"], ["verify"]]
plan.tasks[0].depends_on = ["verify"]
try:
    plan.validate_against(brief)
except ValueError:
    pass
else:
    raise AssertionError("Cyclic intake plan was accepted")
for revision in BriefRevision.objects.all():
    checked_brief(revision)
for saved in ExecutionPlan.objects.select_related("brief_revision"):
    checked_plan(saved, checked_brief(saved.brief_revision))
try:
    urllib.request.urlopen("http://127.0.0.1:8000/api/v1/briefs", timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("Unauthenticated intake read was accepted")
print(f"Intake schema, dependency rejection, and authenticated reads passed; "
      f"{ProductBrief.objects.count()} product briefs retained. No work created or dispatched.")
