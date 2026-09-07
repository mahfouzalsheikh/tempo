"""Verify intake contracts and persisted revisions without creating production work."""
import os
import urllib.error
import urllib.request

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from tempo.contracts.intake import Brief, Plan, digest, starter_plan  # noqa: E402
from tempo.intake import checked_brief, checked_plan  # noqa: E402
from tempo.product_execution import restore_product  # noqa: E402
from tempo_web.models import (  # noqa: E402
    AgentRun,
    BriefRevision,
    ExecutionPlan,
    ProductBrief,
    ValidationAttempt,
)

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
product_runs = 0
for run in AgentRun.objects.all():
    if not (run.execution_plan_id or run.product_snapshot):
        continue
    context = restore_product(run)
    product_runs += 1
    if run.status == "succeeded":
        assert run.phase == "CandidateChecksPassed"
        evidence = run.checkpoints.filter(kind="product_candidate").latest("sequence").payload
        validation = ValidationAttempt.objects.get(pk=evidence["validation_record_id"], run=run)
        assert validation.status == "passed"
        assert evidence["snapshot_digest"] == run.snapshot_digest
        assert evidence["plan_digest"] == digest(Plan.model_validate(context["plan"]))
        assert evidence["brief_digest"] == digest(Brief.model_validate(context["brief"]))
        assert evidence["workspace_fingerprint"] == validation.workspace_fingerprint
        assert evidence["policy_digest"] == validation.policy_digest
        assert evidence["required_check_ids"] == validation.required_check_ids
try:
    urllib.request.urlopen("http://127.0.0.1:8000/api/v1/briefs", timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("Unauthenticated intake read was accepted")
print(f"Intake schema, dependency rejection, and authenticated reads passed; "
      f"{ProductBrief.objects.count()} product briefs and {product_runs} candidate runs retained. "
      "Saved execution and evidence checks passed. No work created or dispatched.")
