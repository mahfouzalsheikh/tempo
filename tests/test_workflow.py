from pathlib import Path

import pytest

from tempo.agent_runtime import ConfiguredModelProvider
from tempo.config import AgentProfileConfig, ModelProviderConfig, build_config
from tempo.domain import Issue
from tempo.errors import ConfigError, WorkflowError
from tempo.workflow import WorkflowStore, load_workflow, render_prompt


def write_workflow(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(text)
    return path


def test_loads_front_matter_and_strict_prompt(tmp_path):
    path = write_workflow(
        tmp_path,
        """---
tracker:
  kind: memory
  active_states: [Todo]
  terminal_states: [Done]
---
Work on {{ issue.identifier }} attempt={{ attempt }}.
""",
    )
    workflow = load_workflow(path)
    config = build_config(workflow.config, path)
    issue = Issue(id="1", identifier="ABC-1", title="Task", state="Todo")
    assert config.polling.interval_ms == 30_000
    assert config.agent.max_tokens_per_run == 1_000_000
    assert config.agent.max_retries == 2
    assert config.validation.max_attempts_per_run == 5
    assert config.review.enabled is False
    assert config.review.merge_method == "squash"
    assert render_prompt(workflow, issue, 2) == "Work on ABC-1 attempt=2."


def test_missing_file_has_typed_error(tmp_path):
    with pytest.raises(WorkflowError) as raised:
        load_workflow(tmp_path / "missing.md")
    assert raised.value.category == "missing_workflow_file"


@pytest.mark.parametrize(
    "front_matter",
    ["- not\n- a\n- map", "tracker: [unterminated"],
)
def test_invalid_front_matter_fails(tmp_path, front_matter):
    path = write_workflow(tmp_path, f"---\n{front_matter}\n---\nprompt")
    with pytest.raises(WorkflowError):
        load_workflow(path)


def test_unknown_prompt_variable_fails(tmp_path):
    path = write_workflow(tmp_path, "Use {{ missing }}")
    workflow = load_workflow(path)
    issue = Issue(id="1", identifier="ABC-1", title="Task", state="Todo")
    with pytest.raises(WorkflowError) as raised:
        render_prompt(workflow, issue, None)
    assert raised.value.category == "prompt_render_error"


def test_environment_workspace_path_and_state_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_WORKSPACES", str(tmp_path / "work"))
    path = write_workflow(
        tmp_path,
        """---
tracker:
  kind: memory
  active_states: [Todo]
  terminal_states: [Done]
workspace:
  root: $TEST_WORKSPACES
agent:
  max_concurrent_agents_by_state:
    " Todo ": 2
    Done: 0
    Bad: nope
---
prompt
""",
    )
    workflow = load_workflow(path)
    config = build_config(workflow.config, path)
    assert config.workspace.root == (tmp_path / "work").resolve()
    assert config.agent.max_concurrent_agents_by_state == {"todo": 2}


def test_overlapping_states_fail(tmp_path):
    path = write_workflow(
        tmp_path,
        "---\ntracker:\n  kind: memory\n  active_states: [Todo]\n  terminal_states: [todo]\n---\nx",
    )
    workflow = load_workflow(path)
    with pytest.raises(ConfigError):
        build_config(workflow.config, path)


def test_review_policy_is_typed_and_normalizes_reviewers(tmp_path):
    path = write_workflow(
        tmp_path,
        """---
tracker:
  kind: memory
  active_states: [Todo]
  terminal_states: [Done]
review:
  enabled: true
  max_turns: 4
  auto_merge: true
  merge_method: rebase
  reviewers: [octocat, " octocat ", maintainer]
  team_reviewers: [platform]
---
prompt
""",
    )
    config = build_config(load_workflow(path).config, path)
    assert config.review.enabled is True
    assert config.review.max_turns == 4
    assert config.review.auto_merge is True
    assert config.review.merge_method == "rebase"
    assert config.review.reviewers == ["octocat", "maintainer"]
    assert config.review.team_reviewers == ["platform"]


def test_typed_graph_validates_references_parallel_fanout_and_cycles(tmp_path):
    path = write_workflow(
        tmp_path,
        """---
tracker:
  kind: memory
  active_states: [Todo]
  terminal_states: [Done]
agents:
  planner:
    role: planner
    completion: turn
  implementer:
    role: implementer
workflow:
  name: specialist-team
  max_parallel_nodes: 2
  nodes:
    - {id: plan, type: agent, agent: planner}
    - {id: api, type: agent, agent: implementer}
    - {id: web, type: agent, agent: implementer}
    - {id: review, type: join, settings: {join: all}}
  edges:
    - {from: plan, to: api}
    - {from: plan, to: web}
    - {from: api, to: review}
    - {from: web, to: review}
---
Work on {{ issue.identifier }}.
""",
    )
    definition = load_workflow(path)
    config = build_config(definition.config, path)
    assert config.workflow.name == "specialist-team"
    assert config.workflow.max_parallel_nodes == 2
    assert [edge.target for edge in config.workflow.edges[:2]] == ["api", "web"]

    definition.config["workflow"]["edges"].append({"from": "review", "to": "plan"})
    with pytest.raises(ConfigError, match="acyclic"):
        build_config(definition.config, path)


def test_model_routes_select_by_role_capability_cost_and_fallback():
    provider = ConfiguredModelProvider()
    config = ModelProviderConfig.model_validate(
        {
            "kind": "openai",
            "model": "default-model",
            "fallbacks": ["last-resort"],
            "routes": [
                {
                    "model": "review-model",
                    "fallbacks": ["review-fallback"],
                    "roles": ["reviewer"],
                    "capabilities": ["code-review"],
                    "max_cost_per_million_tokens": 4.0,
                }
            ],
        }
    )
    profile = AgentProfileConfig(
        role="reviewer",
        capabilities=["code-review"],
        max_model_cost_per_million_tokens=5.0,
    )
    first = provider.resolve(config, profile)
    second = provider.resolve(config, profile, candidate_index=1)
    assert first.model == "review-model"
    assert first.candidates == (
        "review-model",
        "review-fallback",
        "default-model",
        "last-resort",
    )
    assert second.model == "review-fallback"


@pytest.mark.asyncio
async def test_platform_sections_are_validated_and_layered_over_file_config(tmp_path):
    path = write_workflow(
        tmp_path,
        """---
tracker:
  kind: memory
  active_states: [Todo]
  terminal_states: [Done]
---
Keep this prompt for {{ issue.identifier }}.
""",
    )
    store = WorkflowStore(path)
    await store.initialize()
    original = path.read_text()
    await store.update_platform_sections(
        {
            "agents": {
                "reviewer": {
                    "role": "reviewer",
                    "completion": "turn",
                }
            },
            "workflow": {
                "nodes": [{"id": "review", "agent": "reviewer"}],
            },
        }
    )
    definition, config = store.current()
    assert config.workflow.nodes[0].agent == "reviewer"
    assert "Keep this prompt" in definition.prompt_template
    assert path.read_text() == original
