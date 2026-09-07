"""Append-only product briefs and plans; approval never dispatches an agent."""

from uuid import UUID

from django.db import transaction
from django.utils import timezone

from tempo.contracts.intake import Brief, Plan, digest, starter_plan
from tempo_web.models import BriefRevision, ExecutionPlan, ProductBrief, Project


class IntakeConflict(ValueError):
    pass


def checked_brief(revision):
    brief = Brief.model_validate(revision.specification)
    if digest(brief) != revision.digest:
        raise IntakeConflict("The saved brief failed its integrity check.")
    return brief


def checked_plan(plan, brief):
    contract = Plan.model_validate(plan.specification)
    if digest(contract) != plan.digest:
        raise IntakeConflict("The saved plan failed its integrity check.")
    waves = contract.validate_against(brief)
    return contract, waves


def append_revision(product, brief, number, user_id):
    revision = BriefRevision.objects.create(
        brief=product,
        number=number,
        specification=brief.model_dump(mode="json"),
        digest=digest(brief),
        created_by_id=user_id,
    )
    plan = starter_plan(brief)
    ExecutionPlan.objects.create(
        brief_revision=revision,
        number=1,
        specification=plan.model_dump(mode="json"),
        digest=digest(plan),
        generator="starter-v1",
        created_by_id=user_id,
    )
    return revision


@transaction.atomic
def create_brief(*, project_id, specification, user_id, request_key):
    brief = Brief.model_validate(specification)
    key = UUID(str(request_key))
    # Project locking also serializes two first submissions with the same request key.
    project = Project.objects.select_for_update().get(pk=project_id, active=True)
    existing = ProductBrief.objects.filter(request_key=key).first()
    if existing:
        original = existing.revisions.get(number=1)
        if (
            existing.project_id != project_id
            or existing.created_by_id != user_id
            or original.digest != digest(brief)
        ):
            raise IntakeConflict("This submission key belongs to another brief or request.")
        return existing
    product = ProductBrief.objects.create(project=project, created_by_id=user_id, request_key=key)
    append_revision(product, brief, 1, user_id)
    return product


@transaction.atomic
def revise_brief(brief_id, *, expected_revision, specification, user_id):
    brief = Brief.model_validate(specification)
    product = ProductBrief.objects.select_for_update().get(pk=brief_id, project__active=True)
    latest = product.revisions.first()
    if latest.number != expected_revision:
        raise IntakeConflict("This brief changed. Reload it before saving another revision.")
    return append_revision(product, brief, latest.number + 1, user_id)


@transaction.atomic
def revise_plan(brief_id, *, expected_plan_id, specification, user_id):
    product = ProductBrief.objects.select_for_update().get(pk=brief_id, project__active=True)
    revision = product.revisions.first()
    previous = revision.plans.first()
    if previous.pk != expected_plan_id:
        raise IntakeConflict("The brief or plan changed. Reload before saving.")
    brief = checked_brief(revision)
    plan = Plan.model_validate(specification)
    plan.validate_against(brief)
    return ExecutionPlan.objects.create(
        brief_revision=revision,
        number=previous.number + 1,
        specification=plan.model_dump(mode="json"),
        digest=digest(plan),
        generator="operator-v1",
        created_by_id=user_id,
    )


@transaction.atomic
def approve_plan(brief_id, *, expected_plan_id, expected_digest, user_id):
    product = ProductBrief.objects.select_for_update().get(pk=brief_id, project__active=True)
    revision = product.revisions.first()
    plan = revision.plans.first()
    if plan.pk != expected_plan_id or plan.digest != expected_digest:
        raise IntakeConflict("The brief or plan changed. Review the latest plan before approval.")
    brief = checked_brief(revision)
    checked_plan(plan, brief)
    if brief.open_questions:
        raise IntakeConflict("Resolve the open questions in a new brief revision before approval.")
    if not plan.approved_at:
        plan.approved_by_id, plan.approved_at = user_id, timezone.now()
        plan.save(update_fields=["approved_by", "approved_at"])
    return plan
