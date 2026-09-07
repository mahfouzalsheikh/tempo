import copy

import pytest
from test_product_execution import configuration, context

from tempo.config import ServiceConfig
from tempo.intake import IntakeConflict
from tempo.product_execution import compile_product, product_gate_policy, product_issue
from tempo.run_snapshot import snapshot_digest
from tempo_web.product_views import default_profile


@pytest.mark.parametrize(
    "condition,labels,inactive",
    [
        ("issue.label:requires-approval", ["tempo"], True),
        ("issue.label:requires-approval", ["tempo", "requires-approval"], False),
        ("not issue.label:tempo", ["tempo"], True),
        ("not issue.label:tempo", [], False),
        (" ISSUE.LABEL:Requires-Approval ", ["tempo"], True),
        ("succeeded", ["tempo"], False),
        ("failed", ["tempo"], False),
        ("always", ["tempo"], False),
        ("unknown", ["tempo"], False),
        ("issue.label:", [], False),
    ],
)
def test_gate_applicability_uses_exact_saved_label_semantics(tmp_path, condition, labels, inactive):
    saved = context(tmp_path)
    config = saved["source_snapshot"]["config"]
    config["tracker"]["required_labels"] = labels
    config["workflow"]["nodes"].append(
        {"id": "approval", "name": "Sensitive approval", "type": "human_gate"}
    )
    config["workflow"]["edges"] = [
        {"source": "original", "target": "approval", "condition": condition}
    ]
    checked = ServiceConfig.model_validate(config)
    saved["source_snapshot"]["config"] = checked.model_dump(mode="json")
    saved["source_digest"] = snapshot_digest(saved["source_snapshot"])
    original = copy.deepcopy(saved)
    policy = product_gate_policy(checked)
    if inactive:
        from tempo.domain import NodeExecutionState
        from tempo.orchestrator import Orchestrator

        issue = product_issue(saved)
        source = NodeExecutionState("original", "Original", "agent")
        for status in ("pending", "running", "succeeded", "failed", "skipped"):
            source.status = status
            assert not Orchestrator._edge_matches(checked.workflow.edges[0], source, issue)
        assert not policy["active"] and policy["inactive"][0]["id"] == "approval"
        assert compile_product(saved)["config"]["workflow"]["name"] == "product-candidate"
        assert saved == original
    else:
        assert policy["active"] == ["approval"]
        with pytest.raises(IntakeConflict, match="approval gates"):
            compile_product(saved)


def test_gate_with_one_possible_incoming_branch_remains_blocked(tmp_path):
    raw = configuration(tmp_path).model_dump(mode="json")
    raw["workflow"]["nodes"].extend(
        [
            {"id": "another", "agent": "worker"},
            {"id": "approval", "type": "human_gate"},
        ]
    )
    raw["workflow"]["edges"] = [
        {"source": "original", "target": "approval", "condition": "issue.label:absent"},
        {"source": "another", "target": "approval", "condition": "succeeded"},
    ]
    assert product_gate_policy(ServiceConfig.model_validate(raw))["active"] == ["approval"]
    raw["workflow"]["edges"] = []
    assert product_gate_policy(ServiceConfig.model_validate(raw))["active"] == ["approval"]


def test_profile_defaults_recognize_project_role_names(tmp_path):
    config = configuration(tmp_path)
    worker = config.agents["worker"]
    config.agents = {
        "planner": worker.model_copy(update={"role": "technical-planner"}),
        "implementer": worker.model_copy(update={"role": "full-stack-implementer"}),
        "verifier": worker.model_copy(update={"role": "integration-verifier"}),
    }
    assert {
        role: default_profile(config, role)
        for role in ["planner", "implementer", "integrator", "verifier"]
    } == {
        "planner": "planner",
        "implementer": "implementer",
        "integrator": "verifier",
        "verifier": "verifier",
    }
