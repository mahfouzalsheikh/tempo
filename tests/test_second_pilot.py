import copy
import json
import runpy
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from tempo.acceptance_contract import specification
from tempo.build_profiles import build_profile
from tempo.config import build_config
from tempo.contracts.intake import Brief, Plan, digest
from tempo.intake import IntakeConflict, revise_plan
from tempo.persistence import PersistenceStore
from tempo.product_execution import compile_product
from tempo.run_snapshot import capture_snapshot, snapshot_digest
from tempo.workflow import load_workflow
from tempo_web.models import AgentRun, ExecutionPlan, ProductBrief

ROOT = Path(__file__).resolve().parents[1]


def package():
    manifest = json.loads((ROOT / "docs/examples/mini-app-second-pilot.json").read_text())
    definition = load_workflow(ROOT / "workflows/REACT_PILOT.md")
    config = build_config(definition.config, definition.path)
    return manifest, definition, config


def context(manifest, definition, config):
    source = capture_snapshot(definition, config)
    return {
        "schema": 2, "mode": "candidate", "plan_id": 1, "brief_id": 1,
        "brief": manifest["brief"], "plan": manifest["plan"],
        "source_snapshot": source, "source_digest": snapshot_digest(source),
        "bindings": manifest["bindings"], "parallelism": manifest["parallelism"],
        "build_profile": build_profile(manifest["build_target"]),
    }


def test_review_package_compiles_to_isolated_checked_tasks_without_issue_dispatch():
    manifest, definition, config = package()
    brief = Brief.model_validate(manifest["brief"])
    plan = Plan.model_validate(manifest["plan"])
    assert plan.schema_version == 2
    assert plan.validate_against(brief) == [
        ["design"], ["remaining", "group-tips"], ["integrate"], ["verify"],
    ]
    assert config.tracker.kind == "memory" and not config.workflow.require_publication
    assert config.project.slug == "react-supervised-pilot"
    assert config.agent.max_retries == 0
    assert config.agent.max_tokens_per_run == 3_000_000
    saved = context(manifest, definition, config)
    original = copy.deepcopy(saved)
    compiled = compile_product(saved)
    assert saved == original
    nodes = compiled["config"]["workflow"]["nodes"]
    assert [node["workspace"] for node in nodes] == [
        "integration", "isolated", "isolated", "integration", "integration",
    ]
    assert nodes[-1]["settings"]["product_task"]["requires"] == []
    assert nodes[-1]["settings"]["product_task"]["required_decisions"]
    check_ids = [check["id"] for check in compiled["config"]["validation"]["required_checks"]]
    assert check_ids == ["reviewed-pilot-base", "release-mini-tests", "release-mini-build"]


def test_pilot_rejects_original_read_only_planner_conflict_before_dispatch():
    manifest, definition, config = package()
    config.agents[manifest["bindings"]["planner"]].capabilities = []
    with pytest.raises(IntakeConflict, match="design.*repository_write"):
        compile_product(context(manifest, definition, config))


def test_browser_plan_covers_each_criterion_and_actual_svg_regression():
    manifest, _, _ = package()
    brief = Brief.model_validate(manifest["brief"])
    suite = specification(digest(brief), manifest["brief"]["criteria"], manifest["checks"])
    assert suite["schema"] == 2
    assert sum(len(check["steps"]) for check in suite["checks"]) == 35
    actions = suite["checks"][-1]["steps"]
    assert any(action[0] == "upload" and action[-1] == "png-circle-v1" for action in actions)
    assert actions[-1] == ["download", "button", "Download SVG SVG ready", "svg-v1"]


def test_pinned_source_and_repair_scope_are_consistent_with_the_reviewed_plan():
    manifest, _, config = package()
    sha = manifest["source_sha"]
    assert f"git checkout --detach {sha}" in config.hooks.after_create
    assert f'test "$(git rev-parse HEAD)" = {sha}' in config.hooks.after_create
    assert sha in config.hooks.before_run
    assert sha in config.validation.required_checks[0].command
    task = next(task for task in manifest["plan"]["tasks"] if task["id"] == "integrate")
    assert task["required_files"] == manifest["repair"]["paths"]
    assert "export const pilotRepairProbe: string = 1;" in task["instructions"]
    assert manifest["repair"]["max_requests"] == 1
    assert manifest["release"]["replace_existing_target"] is False


@pytest.mark.django_db(transaction=True)
def test_import_creates_only_an_unapproved_draft_and_never_overwrites_operator_edits(monkeypatch):
    manifest, definition, config = package()
    store = PersistenceStore("memory", config=config, definition=definition)
    async_to_sync(store.initialize)()
    user = get_user_model().objects.create_user(username="pilot-operator")
    monkeypatch.setenv("TEMPO_ADMIN_USERNAME", user.username)
    prepare = runpy.run_path(str(ROOT / "scripts/prepare-second-pilot.py"))["prepare"]
    first = prepare(manifest)
    assert prepare(manifest) == first
    assert first["approved"] is False and first["existing_runs"] == 0
    assert ProductBrief.objects.count() == 1 and AgentRun.objects.count() == 0
    saved = ExecutionPlan.objects.get(pk=first["plan_id"])
    assert saved.approved_by_id is None and saved.approved_at is None
    changed = copy.deepcopy(saved.specification)
    changed["tasks"][0]["instructions"] += " Additional operator guidance."
    updated = revise_plan(
        first["brief_id"], expected_plan_id=saved.pk, specification=changed, user_id=user.pk,
    )
    with pytest.raises(ValueError, match="existing pilot plan changed"):
        prepare(manifest)
    updated.refresh_from_db()
    assert updated.specification == changed and AgentRun.objects.count() == 0
