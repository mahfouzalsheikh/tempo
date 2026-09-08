"""Reviewed, append-only repair assignments for stopped product candidates."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync, sync_to_async
from django.db import transaction

from .config import WorkflowNodeConfig
from .domain import LiveSession, NodeExecutionState, utcnow
from .errors import CodexError, LeaseLostError
from .intake import IntakeConflict
from .integration import git, git_environment, inspect_repository
from .product_execution import prepare_product_repository, restore_product
from .run_snapshot import restore_snapshot, snapshot_digest
from .validation import workspace_fingerprint
from .workspace import WorkspaceManager

ACTION = "product_repair"
MAX_REPAIRS = 3


def actions(run):
    rows = list(run.operator_actions.filter(action=ACTION).order_by("pk"))
    for row in rows:
        contract = row.payload.get("contract", {})
        if (
            row.payload.get("digest") != snapshot_digest(contract)
            or contract.get("schema") != 1
            or contract.get("run_id") != run.pk
            or contract.get("snapshot_digest") != run.snapshot_digest
            or contract.get("product_digest") != run.product_snapshot_digest
        ):
            raise IntakeConflict("The saved repair assignment is invalid.")
    return rows


def eligibility(run):
    from tempo_web.models import AgentRun

    context = restore_product(run)
    _, config = restore_snapshot(run.execution_snapshot, run.snapshot_digest)
    if run.status not in {"failed", "paused", "cancelled"} or run.worker_id or run.lease_token:
        raise IntakeConflict("A repair requires a stopped run with no active worker.")
    if run.build_artifacts.exists() or run.checkpoints.filter(kind="product_candidate").exists():
        raise IntakeConflict("This run already has a checked candidate. Start a new plan instead.")
    product = run.execution_plan.brief_revision.brief
    latest = product.revisions.first().plans.first()
    if latest.pk != run.execution_plan_id or not latest.approved_at:
        raise IntakeConflict("Only the current approved plan can receive a repair.")
    if AgentRun.objects.filter(execution_plan__brief_revision__brief=product, status__in=[
        "queued", "running", "retry_scheduled", "paused", "waiting_approval",
    ]).exclude(pk=run.pk).exists():
        raise IntakeConflict("Stop other work on this product before requesting a repair.")
    expected = {node.id for node in config.workflow.nodes}
    completed = set(run.node_runs.filter(status="succeeded").values_list("node_key", flat=True))
    if not expected <= completed or not run.checkpoints.filter(kind="product_base").exists():
        raise IntakeConflict("Finish the approved agent tasks before repairing the final build.")
    if run.validations.count() >= config.validation.max_attempts_per_run:
        raise IntakeConflict("The saved required-check attempt limit is exhausted.")
    if len(actions(run)) >= MAX_REPAIRS:
        raise IntakeConflict("This run has used its three repair requests. Review a new plan.")
    return context, config


async def source_identity(path):
    head = await inspect_repository(path, allow_dirty=True)
    fingerprint = await workspace_fingerprint(path, env=git_environment())
    return {"head": head, "fingerprint": fingerprint}


def review(run):
    from .credentials import CONTROL_PLANE_SECRETS, redact_credentials

    context, config = eligibility(run)
    path = Path(run.workspace_path)
    manager = WorkspaceManager(config.workspace.root, config.hooks)
    if not run.workspace_path or path.is_symlink() or not path.is_dir() or (
        manager.path_for(path.name) != path.resolve()
    ):
        raise IntakeConflict("The saved candidate checkout is unavailable.")
    source = async_to_sync(source_identity)(path)
    base = run.checkpoints.filter(kind="product_base").first().payload["base_sha"]
    async_to_sync(git)(path, "merge-base", "--is-ancestor", base, source["head"])
    validation = run.validations.order_by("-pk").first()
    secrets = [os.environ.get(key, "") for key in CONTROL_PLANE_SECRETS]
    evidence = {
        "run_id": run.pk, "snapshot_digest": run.snapshot_digest,
        "product_digest": run.product_snapshot_digest,
        "attempt": run.attempt, "error": run.error,
        "finished_at": str(run.finished_at), "source": source,
        "validations": list(run.validations.order_by("pk").values("id", "status")),
        "failed_checks": [
            {"check_id": command.check_id, "exit_code": command.exit_code,
             "output": redact_credentials(command.output, secrets)[:4000]}
            for command in validation.commands.exclude(exit_code=0)[:20]
        ] if validation else [],
        "repairs": [row.payload["digest"] for row in actions(run)],
    }
    return {
        "digest": snapshot_digest(evidence), "evidence": evidence,
        "changes": async_to_sync(git)(
            path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        ).decode(errors="replace").replace("\0", "\n")[:8000],
        "profile": context["bindings"]["implementer"],
        "remaining_checks": config.validation.max_attempts_per_run - run.validations.count(),
        "remaining_repairs": MAX_REPAIRS - len(evidence["repairs"]),
    }


def checked_paths(value, context):
    paths = value.splitlines() if isinstance(value, str) else value
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
        raise IntakeConflict("Provide one to twenty file or directory paths, one per line.")
    result = []
    for value in paths:
        if not isinstance(value, str):
            raise IntakeConflict("Repair paths must be text.")
        value = value.strip()
        parts = value.rstrip("/").split("/")
        if (not value or len(value) > 300 or any(p in {"", ".", "..", ".git"} for p in parts)
                or any(c in value for c in "\\*?[]\x00\r\n")
                or (context.get("build_profile") and not value.startswith("mini-app/"))):
            raise IntakeConflict("Use relative paths inside the build target; no globs or .git.")
        result.append(value)
    return sorted(set(result))


@transaction.atomic
def enqueue(store, run_id, *, user_id, idempotency_key, expected_digest, instructions, paths):
    from tempo_web.models import AgentRun, OperatorAction, ProductBrief, RunNode

    source = AgentRun.objects.get(pk=run_id, project_id=store.project_id)
    if not source.execution_plan_id:
        raise IntakeConflict("Only product runs support build repairs.")
    # Same lock order as intake/launch and release scope changes.
    ProductBrief.objects.select_for_update().get(pk=source.execution_plan.brief_revision.brief_id)
    run = AgentRun.objects.select_for_update().get(pk=run_id)
    context = restore_product(run)
    if not isinstance(instructions, str) or not 10 <= len(instructions.strip()) <= 4000:
        raise IntakeConflict("Describe the repair in 10 to 4,000 characters.")
    assignment = {"instructions": instructions.strip(), "paths": checked_paths(paths, context)}
    existing = OperatorAction.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (existing.run_id != run_id or existing.action != ACTION
                or existing.requested_by_id != user_id
                or existing.payload.get("review_digest") != expected_digest
                or existing.payload.get("contract", {}).get("assignment") != assignment):
            raise IntakeConflict("This request key was already used for a different action.")
        return existing
    info = review(run)
    if expected_digest != info["digest"]:
        raise IntakeConflict("The candidate or failure changed. Reload and review the repair.")
    node_id = f"repair-{uuid.uuid4().hex}"
    contract = {
        "schema": 1, "run_id": run.pk, "snapshot_digest": run.snapshot_digest,
        "product_digest": run.product_snapshot_digest, "node_id": node_id,
        "source": info["evidence"]["source"], "assignment": assignment,
        "failure": info["evidence"],
    }
    row = OperatorAction.objects.create(
        run=run, action=ACTION, requested_by_id=user_id, status="applied",
        applied_at=utcnow(), idempotency_key=idempotency_key,
        message="Scoped agent repair queued; fresh required checks follow",
        payload={"contract": contract, "digest": snapshot_digest(contract),
                 "review_digest": expected_digest},
    )
    RunNode.objects.filter(run=run, input__has_key="review_digest",
                           status__in=["pending", "running"]).update(
        status="cancelled", finished_at=utcnow(), error="Superseded by a new reviewed repair.",
    )
    RunNode.objects.create(
        run=run, node_key=node_id, name=f"Build repair {len(actions(run))}", node_type="agent",
        agent_name="product-implementer", role="implementer", input=row.payload,
    )
    run.status, run.phase = "retry_scheduled", "RepairQueued"
    run.available_at, run.finished_at = utcnow(), None
    run.attempt = (run.attempt or 0) + 1
    run.save(update_fields=["status", "phase", "available_at", "finished_at", "attempt"])
    return row


async def verify_result(path, contract):
    head = await inspect_repository(path)
    base = contract["source"]["head"]
    await git(path, "merge-base", "--is-ancestor", base, head)
    # A leading status field preserves filenames with whitespace through git()'s outer strip.
    changed = await git(path, "diff", "--no-renames", "--name-status", "-z", base, head)
    allowed = contract["assignment"]["paths"]
    for raw in changed.split(b"\0")[1::2]:
        if raw and not any(
            os.fsdecode(raw) == scope
            or (scope.endswith("/") and os.fsdecode(raw).startswith(scope))
            for scope in allowed
        ):
            raise IntakeConflict("The repair changed files outside its reviewed paths.")
    return head


async def execute(controller, entry, path, tracker):
    """Run at most one new model turn; interrupted turns need an explicit new review."""
    from tempo_web.models import AgentRun

    run = await AgentRun.objects.aget(pk=entry.run_record_id)
    history = await sync_to_async(actions)(run)
    if not history:
        return
    current = history[-1]
    contract = current.payload["contract"]
    rows = [row async for row in run.node_runs.all()]
    node_row = next(row for row in rows if row.node_key == contract["node_id"])
    for row in rows:
        # Include every historical repair, including failed turns, in lifetime usage.
        entry.node_sessions[row.node_key] = LiveSession(
            codex_input_tokens=max(row.input_tokens, row.output.get("input_tokens", 0)),
            codex_output_tokens=max(row.output_tokens, row.output.get("output_tokens", 0)),
            codex_total_tokens=max(row.total_tokens, row.output.get("total_tokens", 0)),
        )
    entry.token_budget_baseline = sum(s.codex_total_tokens for s in entry.node_sessions.values())
    if node_row.status == "succeeded":
        return
    if node_row.attempt:
        raise CodexError("The repair turn stopped. Review a new repair request before continuing.",
                         category="product_checks_failed")
    if await source_identity(path) != contract["source"]:
        raise CodexError("The reviewed candidate changed before repair. Review it again.",
                         category="product_checks_failed")
    await prepare_product_repository(entry, path, controller.persistence, allow_dirty=True)
    node = WorkflowNodeConfig.model_validate({
        "id": node_row.node_key, "name": node_row.name, "agent": "product-implementer",
        "workspace": "integration",
        "settings": {"product_task": {
            "title": node_row.name, **contract["assignment"], "failure": contract["failure"],
            "constraint": "Repair within the approved brief. Preserve existing features. "
                          "Do not weaken tests. Restore unintended generated changes as needed. "
                          "Commit the fix; Tempo enforces the reviewed paths and reruns checks.",
        }, "product_repair": contract},
    })
    entry.graph_nodes[node.id] = NodeExecutionState(
        node_id=node.id, name=node.name, node_type="agent", agent=node.agent,
        role="implementer", status="running", attempt=1,
    )
    store = controller.persistence
    try:
        await store.start_run_node(run.pk, node.id, 1, lease_token=entry.lease_token)
        output = await controller._execute_agent_node(
            entry.issue, entry.attempt, entry.execution_definition, entry.execution_config,
            node, path, entry.workspace_manager, tracker,
        )
        output.update(source_sha=await verify_result(path, contract),
                      repair_digest=current.payload["digest"])
        await store.finish_run_node(run.pk, node.id, status="succeeded", output=output,
                                    lease_token=entry.lease_token)
    except LeaseLostError:
        raise
    except (Exception, asyncio.CancelledError) as exc:
        await store.finish_run_node(run.pk, node.id, status="failed", error=str(exc),
                                    lease_token=entry.lease_token)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise CodexError(f"Build repair stopped: {exc}", category="product_checks_failed") from exc
    finally:
        entry.graph_nodes.pop(node.id, None)


async def check_candidate(run_id, head):
    from tempo_web.models import AgentRun

    run = await AgentRun.objects.aget(pk=run_id)
    history = await sync_to_async(actions)(run)
    if history:
        latest = history[-1]
        node = await run.node_runs.aget(node_key=latest.payload["contract"]["node_id"])
        if (node.status != "succeeded" or node.output.get("source_sha") != head
                or node.output.get("repair_digest") != latest.payload["digest"]):
            raise CodexError("The candidate no longer matches the completed repair commit.",
                             category="product_checks_failed")


def evidence(run):
    result = []
    for row in actions(run):
        node = run.node_runs.get(node_key=row.payload["contract"]["node_id"])
        if node.status == "succeeded" and node.output.get("repair_digest") != row.payload["digest"]:
            raise IntakeConflict("The completed repair does not match its assignment.")
        result.append({"action_id": row.pk, "digest": row.payload["digest"],
                       "status": node.status, "source_sha": node.output.get("source_sha", "")})
    if result and result[-1]["status"] != "succeeded":
        raise IntakeConflict("The latest repair has not completed.")
    return result
