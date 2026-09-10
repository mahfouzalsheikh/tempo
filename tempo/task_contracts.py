"""Host checks for versioned task requirements and retained deliverable evidence."""

import json

from pydantic import Field

from .contracts.intake import CheckedTask, Contract, Key, Text
from .errors import CodexError
from .intake import IntakeConflict


class Decision(Contract):
    id: Key
    decision: Text
    rationale: Text


class DecisionResult(Contract):
    decisions: list[Decision] = Field(max_length=30)


def check_task_profiles(plan, config, bindings):
    for task in plan.tasks:
        if not isinstance(task, CheckedTask):
            continue
        name = bindings[task.role]
        profile = config.agents[name]
        missing = set(task.requires) - set(profile.capabilities)
        if missing:
            raise IntakeConflict(
                f"{task.id}: profile '{name}' lacks {', '.join(sorted(missing))}. "
                "Select a compatible profile or review its declared capabilities."
            )
        runtime = config.runtime_providers[profile.runtime]
        if "repository_write" in task.requires and runtime.kind == "codex":
            sandbox = runtime.settings.get("thread_sandbox", config.codex.thread_sandbox)
            policy = runtime.settings.get("turn_sandbox_policy", config.codex.turn_sandbox_policy)
            if sandbox == "read-only" or (policy or {}).get("type") == "readOnly":
                raise IntakeConflict(
                    f"{task.id}: profile '{name}' uses a read-only runtime "
                    "but needs repository_write."
                )


RESULT_PROMPT = """
Tempo checks this task's declared requirements before accepting completion.
If requires does not include repository_write, leave the repository unchanged.
Required files must be nonempty regular files committed in this checkout.
If required_decisions is nonempty, your final message must be a JSON object (no Markdown fences):
{"decisions": [{"id": "required-id", "decision": "The decision", "rationale": "Why"}]}
Include exactly one entry per required decision ID. These are reviewable decisions, not proof
that acceptance checks passed.
"""


def completed_message(event):
    """Read complete runtime messages, never the truncated activity display or deltas."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    item = payload.get("item")
    text = None
    if event.get("event") == "item/completed" and isinstance(item, dict):
        if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
            text = item.get("text")
    elif event.get("event") == "assistantMessage/completed":
        text = payload.get("message")
    return text if isinstance(text, str) else None


async def assignment_base(entry, node_id, task, head, persistence):
    """A retry cannot adopt unaccepted edits as a read-only task's new baseline."""
    from tempo_web.models import RunCheckpoint

    key = f"task-base:{entry.run_record_id}:{node_id}"
    previous = await RunCheckpoint.objects.filter(
        run_id=entry.run_record_id, idempotency_key=key,
    ).afirst()
    if previous:
        base = previous.payload["source_sha"]
    else:
        base = head
        await persistence.checkpoint(
            entry.run_record_id, "task_base", {"node_id": node_id, "source_sha": head},
            idempotency_key=key, lease_token=entry.lease_token,
        )
    if "repository_write" not in task["requires"] and head != base:
        raise CodexError(
            f"{node_id}: Restore the original task commit before retrying this read-only task.",
            category="task_deliverable_required",
        )
    return base


async def check_task_result(raw, path, base, final_message):
    from .integration import git, inspect_repository

    task = CheckedTask.model_validate(raw)

    def fail(message):
        return CodexError(f"{task.id}: {message}", category="task_deliverable_required")

    head = await inspect_repository(path)
    if "repository_write" not in task.requires and head != base:
        raise fail("This task did not permit repository changes.")
    files = []
    for name in task.required_files:
        # Filter exact paths, even if Git interprets a supplied name as a directory.
        entries = (await git(path, "ls-tree", "-z", head, "--", name)).split(b"\0")
        entry = next((row for row in entries if row.partition(b"\t")[2] == name.encode()), None)
        if entry is None:
            raise fail(f"Required file '{name}' is missing from the task commit.")
        mode, kind, blob = entry.partition(b"\t")[0].split()
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise fail(f"Required file '{name}' must be a regular file.")
        size = int(await git(path, "cat-file", "-s", blob.decode()))
        if size == 0:
            raise fail(f"Required file '{name}' is empty.")
        files.append({"path": name, "blob": blob.decode(), "size": size})
    decisions = []
    if task.required_decisions:
        try:
            if len(final_message) > 256_000:
                raise ValueError("oversized result")
            result = DecisionResult.model_validate(json.loads(final_message))
            ids = [item.id for item in result.decisions]
            if len(set(ids)) != len(ids) or set(ids) != set(task.required_decisions):
                raise ValueError("decision IDs do not match")
            decisions = result.model_dump(mode="json")["decisions"]
        except (TypeError, ValueError) as exc:
            raise fail(
                "Return the required decisions as the specified final JSON message."
            ) from exc
    return {"schema": 1, "source_sha": head, "files": files, "decisions": decisions}
