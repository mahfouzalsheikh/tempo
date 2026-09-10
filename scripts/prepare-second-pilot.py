"""Import the reviewed-format pilot package as an unapproved draft; never dispatch work.

Run inside the deployed Tempo container with the JSON package path as the only argument.
Creation is attributed to the configured active operator; no approval is recorded.
"""

import json
import os
import sys
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402

from tempo.acceptance_contract import specification  # noqa: E402
from tempo.build_profiles import build_profile  # noqa: E402
from tempo.contracts.intake import Brief, Plan, digest  # noqa: E402
from tempo.intake import create_brief, revise_plan  # noqa: E402
from tempo.product_execution import compile_product  # noqa: E402
from tempo_web.models import Project, WorkflowVersion  # noqa: E402


@transaction.atomic
def prepare(manifest):
    if manifest["schema"] != 1 or manifest["status"] != "draft-awaiting-review":
        raise ValueError("Only the draft pilot package is supported.")
    brief = Brief.model_validate(manifest["brief"])
    plan = Plan.model_validate(manifest["plan"])
    plan.validate_against(brief)
    if plan.schema_version != 2:
        raise ValueError("The second pilot requires checked task contracts.")
    checks = specification(digest(brief), manifest["brief"]["criteria"], manifest["checks"])
    organization, slug = manifest["project"].split("/")
    project = Project.objects.select_for_update().get(
        organization__slug=organization, slug=slug, active=True,
    )
    version = (
        WorkflowVersion.objects.filter(project=project, active=True).order_by("-version").first()
    )
    if version is None:
        raise ValueError("Load the dedicated pilot workflow before importing the draft.")
    compile_product({
        "schema": 2, "mode": "candidate", "plan_id": 0, "brief_id": 0,
        "brief": brief.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
        "source_snapshot": version.execution_snapshot, "source_digest": version.checksum,
        "bindings": manifest["bindings"], "parallelism": manifest["parallelism"],
        "build_profile": build_profile(manifest["build_target"]),
    })
    username = os.environ.get("TEMPO_ADMIN_USERNAME", "")
    if not username:
        raise ValueError("Configure the operator identity before creating a draft.")
    user = get_user_model().objects.get(username=username, is_active=True)
    product = create_brief(
        project_id=project.pk, specification=brief.model_dump(mode="json"),
        user_id=user.pk, request_key=manifest["request_key"],
    )
    revision = product.revisions.first()
    saved = revision.plans.first()
    if revision.digest != digest(brief):
        raise ValueError("The existing pilot brief changed; do not overwrite an operator revision.")
    if saved.digest != digest(plan):
        if saved.generator != "starter-v1" or saved.number != 1 or saved.approved_at:
            raise ValueError(
                "The existing pilot plan changed; review it instead of overwriting it."
            )
        saved = revise_plan(
            product.pk, expected_plan_id=saved.pk, specification=plan.model_dump(mode="json"),
            user_id=user.pk,
        )
    return {
        "brief_id": product.pk, "plan_id": saved.pk, "plan_digest": saved.digest,
        "approved": bool(saved.approved_at), "existing_runs": saved.runs.count(),
        "draft_url": f"http://localhost:8001/ideas/{product.pk}/",
        "configuration_digest": version.checksum,
        "browser_steps": sum(len(check["steps"]) for check in checks["checks"]),
        "action": "Draft prepared; no approval recorded and no work dispatched.",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python prepare-second-pilot.py <package.json>")
    print(json.dumps(prepare(json.loads(Path(sys.argv[1]).read_text())), indent=2))
