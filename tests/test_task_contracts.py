import copy
import json

import pytest
from test_product_execution import checked_specification, context

from tempo.contracts.intake import CheckedTask, Plan, digest
from tempo.errors import CodexError
from tempo.intake import IntakeConflict
from tempo.integration import git, inspect_repository
from tempo.product_execution import compile_product
from tempo.run_snapshot import snapshot_digest
from tempo.task_contracts import check_task_result, completed_message


def task(**changes):
    return {
        "id": "design", "title": "Design", "role": "planner", "instructions": "Decide",
        "criteria": [], "depends_on": [], "requires": [], "required_files": [],
        "required_decisions": [], **changes,
    }


@pytest.mark.parametrize("path", ["../secret", "/tmp/file", ".git/config", "a/../b", "a//b",
                                     "a/./b", ".", "a/", "a\\b", "a\x00b", "a\nb"])
def test_file_contract_rejects_nonliteral_or_escaping_paths(path):
    with pytest.raises(ValueError):
        CheckedTask.model_validate(task(requires=["repository_write"], required_files=[path]))


def test_file_deliverable_requires_explicit_write_requirement():
    with pytest.raises(ValueError, match="repository_write"):
        CheckedTask.model_validate(task(required_files=["design.md"]))


def test_legacy_plan_and_compiled_snapshot_remain_identical(tmp_path):
    saved = context(tmp_path)
    raw = copy.deepcopy(saved["plan"])
    original = compile_product(saved)
    assert Plan.model_validate(raw).model_dump(mode="json") == raw
    assert digest(Plan.model_validate(raw)) == snapshot_digest(raw)
    assert compile_product(saved) == original
    raw["schema_version"] = 2
    with pytest.raises(ValueError, match="explicit requirements"):
        Plan.model_validate(raw)
    raw = checked_specification(saved["plan"])
    raw["schema_version"] = 1
    with pytest.raises(ValueError):
        Plan.model_validate(raw)


@pytest.mark.parametrize("restriction", ["undeclared", "thread", "turn", "override", None])
def test_writing_planner_requires_capable_profile_and_runtime(tmp_path, restriction):
    saved = context(tmp_path)
    saved["plan"] = checked_specification(saved["plan"])
    planner = saved["plan"]["tasks"][0]
    planner.update(requires=["repository_write"], required_files=["design.md"])
    config = saved["source_snapshot"]["config"]
    if restriction != "undeclared":
        config["agents"]["worker"]["capabilities"] = ["repository_write"]
    if restriction == "thread":
        config["codex"]["thread_sandbox"] = "read-only"
    if restriction == "turn":
        config["codex"]["turn_sandbox_policy"] = {"type": "readOnly"}
    if restriction == "override":
        config["runtime_providers"]["codex"]["settings"]["thread_sandbox"] = "read-only"
    saved["source_digest"] = snapshot_digest(saved["source_snapshot"])
    if restriction:
        with pytest.raises(IntakeConflict, match="design.*repository_write"):
            compile_product(saved)
    else:
        compiled = compile_product(saved)
        assert compiled["config"]["workflow"]["nodes"][0]["settings"]["product_task"] == planner


@pytest.fixture
async def repository(tmp_path):
    await git(tmp_path, "init", "--template=", "--initial-branch=main")
    (tmp_path / "base.txt").write_text("base")
    await git(tmp_path, "add", ".")
    await git(tmp_path, "commit", "-m", "Base")
    return tmp_path, await inspect_repository(tmp_path)


@pytest.mark.parametrize("kind", ["missing", "empty", "symlink", "directory", "regular"])
async def test_required_files_are_checked_in_the_commit(repository, kind):
    path, base = repository
    output = path / "design.md"
    if kind == "symlink":
        output.symlink_to("base.txt")
    elif kind == "directory":
        output.mkdir()
        (output / "nested").write_text("not the requested file")
    elif kind != "missing":
        output.write_text("Design" if kind == "regular" else "")
    if kind != "missing":
        await git(path, "add", ".")
        await git(path, "commit", "-m", "Deliverable")
    raw = task(requires=["repository_write"], required_files=["design.md"])
    if kind == "regular":
        result = await check_task_result(raw, path, base, "")
        assert result["source_sha"] == await inspect_repository(path)
        assert result["files"] == [{"path": "design.md", "size": 6,
                                   "blob": (await git(path, "rev-parse", "HEAD:design.md"))
                                   .decode().strip()}]
    else:
        with pytest.raises(CodexError, match="Required file"):
            await check_task_result(raw, path, base, "")


async def test_undeclared_repository_changes_cannot_be_accepted(repository):
    path, base = repository
    (path / "base.txt").write_text("changed")
    await git(path, "add", ".")
    await git(path, "commit", "-m", "Unexpected edit")
    with pytest.raises(CodexError, match="did not permit repository changes"):
        await check_task_result(task(), path, base, "")


@pytest.mark.parametrize("problem", ["missing", "duplicate", "unknown", "blank", "prose", None])
async def test_decision_results_require_exact_ids_and_retain_full_content(repository, problem):
    path, base = repository
    entry = {"id": "approach", "decision": "A" * 3000, "rationale": "B" * 3000}
    entries = [entry]
    if problem == "missing":
        entries = []
    elif problem == "duplicate":
        entries.append(entry)
    elif problem == "unknown":
        entry["id"] = "other"
    elif problem == "blank":
        entry["rationale"] = " "
    message = json.dumps({"decisions": entries}) if problem != "prose" else "Done"
    raw = task(required_decisions=["approach"])
    if problem:
        with pytest.raises(CodexError, match="required decisions"):
            await check_task_result(raw, path, base, message)
    else:
        event = {"event": "item/completed", "payload": {"item": {
            "type": "agentMessage", "text": message,
        }}}
        result = await check_task_result(raw, path, base, completed_message(event))
        assert result["decisions"] == entries
        event["payload"]["item"]["phase"] = "commentary"
        assert completed_message(event) is None
        assert completed_message({"event": "item/agentMessage/delta",
                                  "payload": {"delta": message}}) is None
        assert completed_message({"event": "assistantMessage/completed",
                                  "payload": {"message": message}}) == message
