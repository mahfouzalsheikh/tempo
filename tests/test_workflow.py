from pathlib import Path

import pytest

from tempo.config import build_config
from tempo.domain import Issue
from tempo.errors import ConfigError, WorkflowError
from tempo.workflow import load_workflow, render_prompt


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
