import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client

from tempo.contracts.intake import Brief, Plan, digest, starter_plan
from tempo.intake import IntakeConflict, approve_plan, create_brief, revise_brief, revise_plan
from tempo_web.models import (
    AgentRun,
    BriefRevision,
    ExecutionPlan,
    Organization,
    ProductBrief,
    Project,
)


def specification():
    return {
        "title": "Booking application",
        "goal": "Prevent conflicting reservations",
        "users": "Visitors and reception staff",
        "scope": "Availability and reservations",
        "exclusions": "Payments",
        "constraints": "Use existing project conventions",
        "target": "A browser application in a preview environment",
        "criteria": [
            {
                "id": "AC-1",
                "outcome": "A visitor can reserve an available slot",
                "verification": "Submit a reservation and confirm it appears in the schedule",
            },
            {
                "id": "AC-2",
                "outcome": "A slot cannot be reserved twice",
                "verification": "Submit simultaneous reservations and assert exactly one succeeds",
            },
        ],
    }


def test_starter_plan_preserves_criteria_and_sequences_independent_verification():
    brief = Brief.model_validate(specification())
    plan = starter_plan(brief)
    assert plan.validate_against(brief) == [
        ["design"],
        ["build-1", "build-2"],
        ["integrate"],
        ["verify"],
    ]
    assert plan.brief_digest == digest(brief)
    assert brief.criteria[1].verification.startswith("Submit simultaneous")
    assert digest(plan) == digest(starter_plan(brief))


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "cycle",
        "missing",
        "uncovered",
        "early",
        "unknown_criterion",
        "wrong_brief",
        "extra",
        "role",
    ],
)
def test_invalid_plans_cannot_be_handoffs(failure):
    brief = Brief.model_validate(specification())
    raw = starter_plan(brief).model_dump()
    if failure == "duplicate":
        raw["tasks"][1]["id"] = "design"
    elif failure == "cycle":
        raw["tasks"][0]["depends_on"] = ["verify"]
    elif failure == "missing":
        raw["tasks"][1]["depends_on"] = ["nonexistent"]
    elif failure == "uncovered":
        raw["tasks"][-1]["criteria"] = ["AC-1"]
    elif failure == "early":
        raw["tasks"][-1]["depends_on"] = ["design"]
    elif failure == "unknown_criterion":
        raw["tasks"][1]["criteria"] = ["AC-999"]
    elif failure == "wrong_brief":
        raw["brief_digest"] = "a" * 64
    elif failure == "extra":
        raw["run_now"] = True
    else:
        raw["tasks"][1]["role"] = "root"
    with pytest.raises(ValueError):
        Plan.model_validate(raw).validate_against(brief)


@pytest.fixture
def operator(db):
    user = get_user_model().objects.create_user(
        username="product-operator", password="test-password"
    )
    org = Organization.objects.create(name="Factory", slug="factory")
    project = Project.objects.create(organization=org, name="Booking", slug="booking")
    return user, project


def create(operator):
    user, project = operator
    return create_brief(
        project_id=project.pk,
        specification=specification(),
        user_id=user.pk,
        request_key=uuid.uuid4(),
    )


def test_revisions_preserve_approved_history_and_require_new_review(operator):
    user, _ = operator
    product = create(operator)
    original = product.revisions.first()
    first_plan = original.plans.first()
    approved = approve_plan(
        product.pk,
        expected_plan_id=first_plan.pk,
        expected_digest=first_plan.digest,
        user_id=user.pk,
    )
    old_payload = copy.deepcopy(approved.specification)
    changed_plan = copy.deepcopy(old_payload)
    changed_plan["tasks"][1]["instructions"] = (
        "Use the agreed reservation endpoint and add API tests"
    )
    next_plan = revise_plan(
        product.pk, expected_plan_id=first_plan.pk, specification=changed_plan, user_id=user.pk
    )
    assert next_plan.number == 2 and not next_plan.approved_at
    first_plan.refresh_from_db()
    assert first_plan.approved_at and first_plan.specification == old_payload
    with pytest.raises(IntakeConflict):
        approve_plan(
            product.pk,
            expected_plan_id=first_plan.pk,
            expected_digest=first_plan.digest,
            user_id=user.pk,
        )
    changed_brief = specification()
    changed_brief["scope"] += "; staff may cancel reservations"
    revised = revise_brief(
        product.pk, expected_revision=1, specification=changed_brief, user_id=user.pk
    )
    assert revised.number == 2
    assert revised.specification["criteria"][0]["id"] == "AC-1"
    assert not revised.plans.first().approved_at
    original.refresh_from_db()
    assert original.specification == Brief.model_validate(specification()).model_dump()
    assert AgentRun.objects.count() == 0
    with pytest.raises(IntakeConflict):
        revise_plan(
            product.pk, expected_plan_id=next_plan.pk, specification=changed_plan, user_id=user.pk
        )
    with pytest.raises(IntakeConflict):
        revise_brief(product.pk, expected_revision=1, specification=changed_brief, user_id=user.pk)


@pytest.mark.parametrize("failure", ["open_questions", "brief_tamper", "plan_tamper"])
def test_approval_checks_exact_contracts_and_open_questions(operator, failure):
    user, project = operator
    spec = specification()
    if failure == "open_questions":
        spec["open_questions"] = "Which timezone defines availability?"
    product = create_brief(
        project_id=project.pk, specification=spec, user_id=user.pk, request_key=uuid.uuid4()
    )
    revision = product.revisions.first()
    plan = revision.plans.first()
    if failure == "brief_tamper":
        raw = revision.specification
        raw["scope"] = "Changed behind the operator's back"
        BriefRevision.objects.filter(pk=revision.pk).update(specification=raw)
    elif failure == "plan_tamper":
        raw = plan.specification
        raw["tasks"][1]["instructions"] = "Changed behind the operator's back"
        ExecutionPlan.objects.filter(pk=plan.pk).update(specification=raw)
    with pytest.raises(IntakeConflict):
        approve_plan(
            product.pk, expected_plan_id=plan.pk, expected_digest=plan.digest, user_id=user.pk
        )
    plan.refresh_from_db()
    assert plan.approved_at is None
    if failure != "open_questions":
        client = Client()
        client.force_login(user)
        assert client.get(f"/api/v1/briefs/{product.pk}").status_code == 409


def test_creation_is_idempotent_and_conflicting_reuse_is_rejected(operator):
    user, project = operator
    kwargs = dict(
        project_id=project.pk,
        user_id=user.pk,
        request_key=uuid.uuid4(),
        specification=specification(),
    )
    product = create_brief(**kwargs)
    assert create_brief(**kwargs).pk == product.pk
    kwargs["specification"]["title"] = "Something else"
    with pytest.raises(IntakeConflict):
        create_brief(**kwargs)
    assert (
        ProductBrief.objects.count()
        == BriefRevision.objects.count()
        == ExecutionPlan.objects.count()
    )


def test_api_authentication_validation_history_and_stale_approval(operator):
    user, project = operator
    client = Client()
    assert client.get("/api/v1/briefs").status_code == 401
    assert client.get("/ideas/").status_code == 302
    client.force_login(user)
    payload = {"project_id": project.pk, "request_key": str(uuid.uuid4()), "brief": specification()}
    response = client.post("/api/v1/briefs", data=payload, content_type="application/json")
    assert response.status_code == 201
    saved = response.json()
    assert saved["execution_started"] is False
    url = f"/api/v1/briefs/{saved['id']}"
    assert client.get(url).json()["brief_digest"] == saved["brief_digest"]
    assert (
        client.post(
            url + "/approve",
            data={"expected_plan_id": saved["plan_id"], "expected_digest": "old"},
            content_type="application/json",
        ).status_code
        == 409
    )
    approval = client.post(
        url + "/approve",
        data={"expected_plan_id": saved["plan_id"], "expected_digest": saved["plan_digest"]},
        content_type="application/json",
    )
    assert approval.status_code == 200 and approval.json()["approved_at"]
    payload["brief"]["criteria"] = []
    assert (
        client.post(
            url,
            data={"expected_revision": 1, "brief": payload["brief"]},
            content_type="application/json",
        ).status_code
        == 400
    )
    assert BriefRevision.objects.count() == 1
    assert client.get(f"/ideas/{saved['id']}/").status_code == 200
    assert client.get(f"/ideas/{saved['id']}/?revision=99").status_code == 404
    revised = specification()
    revised["title"] = "Booking application with cancellations"
    assert (
        client.post(
            url, data={"expected_revision": 1, "brief": revised}, content_type="application/json"
        ).status_code
        == 201
    )
    historical = client.get(url + "?revision=1&plan=1").json()
    assert historical["brief"]["title"] == saved["brief"]["title"]
    assert historical["approved_at"] and not historical["is_current"]
    assert client.get(url).json()["approved_at"] is None
    assert AgentRun.objects.count() == 0


def brief_form_payload(project, key):
    spec = specification()
    return {
        **{name: value for name, value in spec.items() if name != "criteria"},
        "project": project.pk,
        "request_key": str(key),
        "criteria-TOTAL_FORMS": "2",
        "criteria-INITIAL_FORMS": "0",
        "criteria-MIN_NUM_FORMS": "1",
        "criteria-MAX_NUM_FORMS": "30",
        **{
            f"criteria-{index}-{field}": criterion[key]
            for index, criterion in enumerate(spec["criteria"])
            for field, key in [
                ("criterion_id", "id"),
                ("outcome", "outcome"),
                ("verification", "verification"),
            ]
        },
    }


def test_browser_forms_create_and_review_without_json_and_enforce_csrf(operator):
    user, project = operator
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.get("/ideas/new/").status_code == 200
    payload = brief_form_payload(project, uuid.uuid4())
    assert client.post("/ideas/new/", data=payload).status_code == 403
    payload["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
    response = client.post("/ideas/new/", data=payload)
    assert response.status_code == 302
    product = ProductBrief.objects.get()
    assert client.get(response.url).status_code == 200
    assert client.get(f"/ideas/{product.pk}/edit/").status_code == 200
    assert client.get(f"/ideas/{product.pk}/plan/").status_code == 200
    assert client.post("/ideas/new/", data=payload).status_code == 302
    assert ProductBrief.objects.count() == 1
    plan = product.revisions.first().plans.first()
    assert (
        client.post(
            f"/ideas/{product.pk}/approve/",
            data={
                "expected_plan_id": plan.pk,
                "expected_digest": plan.digest,
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            },
        ).status_code
        == 302
    )
    assert b"Plan approved" in client.get(f"/ideas/{product.pk}/").content


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks")
def test_competing_brief_revisions_cannot_overwrite_each_other(operator):
    user, _ = operator
    product = create(operator)
    barrier = Barrier(2, timeout=10)

    def revise(index):
        try:
            barrier.wait()
            spec = specification()
            spec["goal"] += f" {index}"
            revise_brief(product.pk, expected_revision=1, specification=spec, user_id=user.pk)
            return "saved"
        except IntakeConflict:
            return "conflict"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, [1, 2]))
    assert sorted(results) == ["conflict", "saved"]
    assert product.revisions.count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks")
def test_duplicate_submissions_create_one_product_and_one_starter_plan(operator):
    user, project = operator
    barrier = Barrier(2, timeout=10)
    request_key = uuid.uuid4()

    def submit(_index):
        try:
            barrier.wait()
            return create_brief(
                project_id=project.pk,
                specification=specification(),
                user_id=user.pk,
                request_key=request_key,
            ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [1, 2]))
    assert results[0] == results[1]
    assert ProductBrief.objects.count() == 1
    assert BriefRevision.objects.count() == 1
    assert ExecutionPlan.objects.count() == 1
