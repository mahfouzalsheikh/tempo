from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from .config import ServiceConfig
from .credentials import CONTROL_PLANE_SECRETS, process_environment
from .domain import Issue
from .errors import CodexError
from .process import stop_process_group
from .trackers.base import Tracker
from .validation import ProjectValidator, clean_workspace_head, workspace_fingerprint
from .workspace import WorkspaceManager

log = structlog.get_logger(__name__)
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class CodexSession:
    process: asyncio.subprocess.Process
    thread_id: str
    workspace: Path
    inbox: asyncio.Queue[dict[str, Any]]
    reader_task: asyncio.Task[None]
    next_request_id: int = 3
    role: str = "implementation"
    validation_fingerprint: str | None = None
    validation_head_sha: str | None = None
    validation_policy_digest: str | None = None
    completion_disposition: str | None = None
    review_decision: str | None = None
    review_summary: str | None = None
    review_head_sha: str | None = None
    resumed: bool = False
    resume_failure: str | None = None
    usage_baseline_input: int = 0
    usage_baseline_output: int = 0
    usage_baseline_total: int = 0
    usage_is_cumulative: bool | None = None
    compacted: bool = False


class CodexAppServer:
    """Version-tolerant Codex app-server JSONL client."""

    def __init__(
        self,
        config: ServiceConfig,
        workspace_manager: WorkspaceManager,
        tracker: Tracker,
        on_event: EventCallback,
        approval_callback: ApprovalCallback | None = None,
        enabled_tools: set[str] | None = None,
    ) -> None:
        self.config = config.codex
        self.validation_enabled = config.validation.enabled
        self.workspace_manager = workspace_manager
        self.tracker = tracker
        tracker.bind_validation_policy(config.validation)
        self.on_event = on_event
        self.approval_callback = approval_callback
        self.enabled_tools = enabled_tools
        self.validator = ProjectValidator(
            config.validation,
            workspace_manager,
            on_event,
            tracker.secret_environment_names(),
        )

    async def start_session(
        self,
        workspace: Path,
        *,
        role: str = "implementation",
        resume_thread_id: str | None = None,
        usage_baseline: dict[str, int] | None = None,
    ) -> CodexSession:
        await self.tracker.assert_ownership()
        if role not in {"implementation", "review"}:
            raise CodexError(f"unsupported agent role: {role}", category="invalid_agent_role")
        self.workspace_manager.assert_contained(workspace)
        # This is a fast local safety check performed once per worker.
        if workspace.resolve(strict=True) == self.workspace_manager.root.resolve(  # noqa: ASYNC240
            strict=True
        ):
            raise CodexError("agent cwd cannot be workspace root", category="invalid_workspace_cwd")
        environment = process_environment(
            self.config.environment,
            forbidden=CONTROL_PLANE_SECRETS | self.tracker.secret_environment_names()
            | {self.validator.config.runner_token[1:]},
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "--noprofile", "--norc", "-c",
                f"exec {self.config.command}",
                cwd=workspace,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CodexError(
                "bash or Codex executable not found", category="codex_not_found"
            ) from exc
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        reader_task = asyncio.create_task(self._read_stdout(process, inbox))
        asyncio.create_task(self._read_stderr(process))
        baseline = usage_baseline or {}
        session = CodexSession(
            process,
            "",
            workspace,
            inbox,
            reader_task,
            role=role,
            usage_baseline_input=int(baseline.get("input_tokens", 0)),
            usage_baseline_output=int(baseline.get("output_tokens", 0)),
            usage_baseline_total=int(baseline.get("total_tokens", 0)),
        )
        try:
            await self._send(
                session,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "tempo_python",
                            "title": "Tempo Python",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            await self._response(session, 1, self.config.read_timeout_ms)
            await self._send(session, {"method": "initialized", "params": {}})
            tracker_tools = self.tracker.agent_tool_specs()
            if self.enabled_tools is not None:
                tracker_tools = [
                    spec for spec in tracker_tools if spec.get("name") in self.enabled_tools
                ]
            dynamic_tools = [*tracker_tools]
            if self.validation_enabled and self._tool_enabled("project_validation"):
                dynamic_tools.append(self.validator.tool_spec())
            if role == "review":
                dynamic_tools.append(self._review_tool_spec())
            elif self._tool_enabled("tempo_complete"):
                dynamic_tools.append(self._completion_tool_spec())
            params: dict[str, Any] = {
                "cwd": str(workspace),
                "approvalPolicy": self.config.approval_policy,
                "sandbox": self.config.thread_sandbox,
                "dynamicTools": dynamic_tools,
            }
            if self.config.model:
                params["model"] = self.config.model
            request_id = 2
            if resume_thread_id:
                resume_params = {"threadId": resume_thread_id, **params}
                session.resumed = True
                await self._send(
                    session,
                    {"method": "thread/resume", "id": request_id, "params": resume_params},
                )
                try:
                    result = await self._response(session, request_id, self.config.read_timeout_ms)
                except CodexError as exc:
                    session.resumed = False
                    session.resume_failure = str(exc)
                    await self.on_event(
                        {
                            "event": "thread_resume_failed",
                            "thread_id": resume_thread_id,
                            "reason": str(exc),
                        }
                    )
                    request_id += 1
                    await self._send(
                        session,
                        {"method": "thread/start", "id": request_id, "params": params},
                    )
                    result = await self._response(session, request_id, self.config.read_timeout_ms)
            else:
                await self._send(
                    session,
                    {"method": "thread/start", "id": request_id, "params": params},
                )
                result = await self._response(session, request_id, self.config.read_timeout_ms)
            thread_id = result.get("thread", {}).get("id")
            if not thread_id:
                method = "thread/resume" if session.resumed else "thread/start"
                raise CodexError(f"{method} returned no thread id", category="response_error")
            session.thread_id = str(thread_id)
            session.next_request_id = request_id + 1
            if session.resumed:
                await self.on_event(
                    {
                        "event": "thread_resumed",
                        "thread_id": session.thread_id,
                        "codex_app_server_pid": str(session.process.pid),
                    }
                )
            return session
        except BaseException:
            await self.stop_session(session)
            raise

    async def run_turn(self, session: CodexSession, prompt: str, issue: Issue) -> dict[str, Any]:
        await self.tracker.assert_ownership()
        request_id = session.next_request_id
        session.next_request_id += 1
        sandbox_policy = self.config.turn_sandbox_policy or {
            "type": "workspaceWrite",
            "writableRoots": [str(session.workspace)],
            "networkAccess": False,
        }
        turn_params: dict[str, Any] = {
            "threadId": session.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(session.workspace),
            "title": f"{issue.identifier}: {issue.title}",
            "approvalPolicy": self.config.approval_policy,
            "sandboxPolicy": sandbox_policy,
        }
        if self.config.model:
            turn_params["model"] = self.config.model
        await self._send(
            session,
            {
                "method": "turn/start",
                "id": request_id,
                "params": turn_params,
            },
        )
        result = await self._response(session, request_id, self.config.read_timeout_ms)
        turn_id = result.get("turn", {}).get("id")
        if not turn_id:
            raise CodexError("turn/start returned no turn id", category="response_error")
        await self.on_event(
            {
                "event": "session_started",
                "session_id": f"{session.thread_id}-{turn_id}",
                "thread_id": session.thread_id,
                "turn_id": str(turn_id),
                "codex_app_server_pid": str(session.process.pid),
            }
        )

        while True:
            message = await self._next_message(session, self.config.turn_timeout_ms)
            method = message.get("method", "")
            if "id" in message and method:
                await self._handle_server_request(session, message, issue)
                continue
            event = self._normalize_usage_for_attempt(session, self._event_from_message(message))
            await self.on_event(event)
            if method == "turn/completed":
                turn = message.get("params", {}).get("turn", {})
                status = turn.get("status", "completed")
                if status in {"failed", "interrupted", "cancelled"}:
                    error = turn.get("error") or {}
                    error_message = str(error.get("message") or "").strip()
                    error_code = "".join(
                        character
                        for character in str(error.get("codexErrorInfo") or "").lower()
                        if character.isalnum()
                    )
                    if error_code == "usagelimitexceeded" or (
                        "workspace" in error_message.lower()
                        and "out of credits" in error_message.lower()
                    ):
                        raise CodexError(
                            error_message or "Codex provider usage limit exceeded",
                            category="provider_usage_limit",
                        )
                    raise CodexError(f"turn ended with status {status}", category="turn_failed")
                return message
            if method in {"turn/failed", "turn/cancelled"}:
                raise CodexError(
                    f"{method}: {message.get('params')}",
                    category=method.replace("/", "_"),
                )

    async def compact_session(self, session: CodexSession) -> None:
        """Compact a resumed thread without charging maintenance usage to the new attempt."""
        request_id = session.next_request_id
        session.next_request_id += 1
        await self._send(
            session,
            {
                "method": "thread/compact/start",
                "id": request_id,
                "params": {"threadId": session.thread_id},
            },
        )
        response_received = False
        compaction_completed = False
        turn_completed = False
        latest_thread_usage = {
            "input_tokens": session.usage_baseline_input,
            "output_tokens": session.usage_baseline_output,
            "total_tokens": session.usage_baseline_total,
        }
        while True:
            message = await self._next_message(session, self.config.turn_timeout_ms)
            if message.get("id") == request_id:
                if message.get("error"):
                    raise CodexError(
                        f"thread compaction failed: {message['error']}",
                        category="thread_compaction_failed",
                    )
                response_received = True
                if compaction_completed and turn_completed:
                    break
                continue
            method = str(message.get("method", ""))
            if "id" in message and method:
                await self._send(
                    session,
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Server requests are unavailable during compaction",
                        },
                    },
                )
                continue
            event = self._event_from_message(message)
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                latest_thread_usage = {
                    "input_tokens": int(raw_usage.get("input_tokens", 0)),
                    "output_tokens": int(raw_usage.get("output_tokens", 0)),
                    "total_tokens": int(raw_usage.get("total_tokens", 0)),
                }
                event = {
                    **event,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                    "thread_usage": latest_thread_usage,
                }
            await self.on_event(event)
            item = message.get("params", {}).get("item", {})
            if (
                method == "item/completed"
                and isinstance(item, dict)
                and item.get("type") == "contextCompaction"
            ):
                compaction_completed = True
            if method in {"turn/failed", "turn/cancelled"}:
                raise CodexError(
                    f"thread compaction ended with {method}",
                    category="thread_compaction_failed",
                )
            if method == "turn/completed":
                turn_completed = True
                if response_received and compaction_completed:
                    break
        session.usage_baseline_input = latest_thread_usage["input_tokens"]
        session.usage_baseline_output = latest_thread_usage["output_tokens"]
        session.usage_baseline_total = latest_thread_usage["total_tokens"]
        session.usage_is_cumulative = None
        session.compacted = True

    @staticmethod
    def _normalize_usage_for_attempt(
        session: CodexSession, event: dict[str, Any]
    ) -> dict[str, Any]:
        """Convert a resumed thread's cumulative usage into this attempt's usage."""
        usage = event.get("usage")
        if not session.resumed or not isinstance(usage, dict):
            return event
        raw_total = int(usage.get("total_tokens", 0))
        if session.usage_is_cumulative is None:
            if raw_total <= 0:
                return event
            session.usage_is_cumulative = (
                session.usage_baseline_total > 0 and raw_total >= session.usage_baseline_total
            )
        if not session.usage_is_cumulative:
            return {
                **event,
                "thread_usage": {
                    "input_tokens": (
                        session.usage_baseline_input
                        + int(usage.get("input_tokens", 0))
                    ),
                    "output_tokens": (
                        session.usage_baseline_output
                        + int(usage.get("output_tokens", 0))
                    ),
                    "total_tokens": session.usage_baseline_total + raw_total,
                },
            }
        normalized = {
            "input_tokens": max(
                0,
                int(usage.get("input_tokens", 0)) - session.usage_baseline_input,
            ),
            "output_tokens": max(
                0,
                int(usage.get("output_tokens", 0)) - session.usage_baseline_output,
            ),
            "total_tokens": max(0, raw_total - session.usage_baseline_total),
        }
        return {
            **event,
            "usage": {**usage, **normalized},
            "thread_usage": {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": raw_total,
            },
        }

    async def _handle_server_request(
        self, session: CodexSession, message: dict[str, Any], issue: Issue
    ) -> None:
        await self.tracker.assert_ownership()
        method = message.get("method")
        request_id = message["id"]
        if method == "item/tool/call":
            params = message.get("params", {})
            name = params.get("tool") or params.get("name")
            arguments = params.get("arguments") or {}
            if not self._tool_enabled(str(name)):
                result = {
                    "success": False,
                    "output": f"Tool {name} is not enabled for this agent profile.",
                    "contentItems": [],
                }
                await self._send(session, {"id": request_id, "result": result})
                await self.on_event(
                    {
                        "event": "tool_call_rejected",
                        "tool": str(name),
                        "arguments": arguments,
                    }
                )
                return
            mutating_tool_call = name in {"github_publish", "github_comment"} or (
                name == "github_api" and str(arguments.get("method", "GET")).upper()
                not in {"GET", "HEAD"}
            )
            if (
                mutating_tool_call
                and self.config.approval_policy != "never"
                and self.approval_callback
            ):
                outcome = await self.approval_callback(f"tool:{name}", message)
                if not outcome.get("approved"):
                    result = {
                        "success": False,
                        "output": "The operator rejected this tool call.",
                        "contentItems": [],
                    }
                    await self._send(session, {"id": request_id, "result": result})
                    await self.on_event(
                        {
                            "event": "tool_call_rejected",
                            "tool": str(name),
                            "arguments": arguments,
                        }
                    )
                    return
                edited = outcome.get("edited_arguments")
                if isinstance(edited, dict) and edited:
                    arguments = edited
            if name == "project_validation" and self.validation_enabled:
                result = await self._execute_project_validation(
                    session,
                    arguments,
                    issue,
                )
            elif name == "tempo_complete":
                if session.role != "implementation":
                    result = {
                        "success": False,
                        "output": "Only the implementation agent may complete without changes.",
                        "contentItems": [],
                    }
                    await self._send(session, {"id": request_id, "result": result})
                    return
                reason = str(arguments.get("reason", "")).strip()
                fingerprint_matches = not self.validation_enabled or (
                    getattr(session, "validation_policy_digest", None)
                    == self.validator.config.policy_digest
                    and session.validation_fingerprint is not None
                    and session.validation_fingerprint
                    == await self._workspace_fingerprint(session.workspace)
                )
                if not reason:
                    result = {
                        "success": False,
                        "output": "A concrete completion reason is required.",
                        "contentItems": [],
                    }
                elif not fingerprint_matches or not await self.tracker.unchanged_from_base():
                    result = {
                        "success": False,
                        "output": (
                            "No-change completion requires unchanged validation and a clean tree "
                            "identical to the task's recorded base. Publish implementation changes."
                        ),
                        "contentItems": [],
                    }
                else:
                    session.completion_disposition = reason
                    await self.on_event({"event": "no_change_completed", "reason": reason})
                    result = {
                        "success": True,
                        "output": "Tempo recorded the ticket as complete without code changes.",
                        "contentItems": [],
                    }
            elif name == "tempo_review":
                decision = str(arguments.get("decision", "")).strip().lower()
                summary = str(arguments.get("summary", "")).strip()
                head_sha = (
                    await clean_workspace_head(session.workspace) if decision == "approve" else None
                )
                fingerprint_matches = not self.validation_enabled or (
                    getattr(session, "validation_policy_digest", None)
                    == self.validator.config.policy_digest
                    and session.validation_fingerprint is not None
                    and session.validation_fingerprint
                    == await self._workspace_fingerprint(session.workspace)
                )
                if session.role != "review":
                    result = {
                        "success": False,
                        "output": "Only the independent review agent may record a review.",
                        "contentItems": [],
                    }
                elif decision not in {"approve", "human_review"}:
                    result = {
                        "success": False,
                        "output": "decision must be approve or human_review.",
                        "contentItems": [],
                    }
                elif not summary:
                    result = {
                        "success": False,
                        "output": "A concrete review summary is required.",
                        "contentItems": [],
                    }
                elif decision == "approve" and (
                    not fingerprint_matches
                    or not head_sha
                    or (
                        self.validation_enabled
                        and head_sha != session.validation_head_sha
                    )
                ):
                    result = {
                        "success": False,
                        "output": (
                            "Commit all review changes, then run project_validation on the clean "
                            "commit before approving. The reviewed commit must still match that "
                            "validation and the published pull-request head."
                        ),
                        "contentItems": [],
                    }
                else:
                    session.review_decision = decision
                    session.review_summary = summary
                    session.review_head_sha = head_sha
                    await self.on_event(
                        {
                            "event": "review_completed",
                            "decision_id": uuid.uuid4().hex,
                            "decision": decision,
                            "summary": summary,
                            "review_head_sha": head_sha,
                            "validation_fingerprint": session.validation_fingerprint,
                            "validation_policy_digest": self.validator.config.policy_digest,
                        }
                    )
                    result = {
                        "success": True,
                        "output": (
                            "Tempo recorded the independent review decision. "
                            "The control plane will now apply merge and human-review policy."
                        ),
                        "contentItems": [],
                    }
            elif name == "github_publish":
                result = await self.tracker.publish_candidate(
                    arguments, issue,
                    session.validation_fingerprint if self.validation_enabled else None,
                )
            else:
                if (
                    name == "github_api" and mutating_tool_call
                    and session.validation_fingerprint
                    and session.validation_fingerprint
                    != await self._workspace_fingerprint(session.workspace)
                ):
                    session.validation_fingerprint = None
                    self.tracker.revoke_publication(issue.id)
                    result = {
                        "success": False,
                        "output": (
                            "The workspace changed after validation. Run project_validation again "
                            "before writing to GitHub."
                        ),
                        "contentItems": [],
                    }
                    await self.on_event({"event": "validation_invalidated"})
                else:
                    result = await self.tracker.execute_agent_tool(str(name), arguments, issue)
            await self._send(session, {"id": request_id, "result": result})
            await self.on_event(
                {
                    "event": "tool_call_completed",
                    "tool": str(name),
                    "arguments": arguments,
                    "success": bool(result.get("success")),
                    "output": str(result.get("output", ""))[:4000],
                    **(
                        {"review_head_sha": session.review_head_sha}
                        if name == "tempo_review" and result.get("success") else {}
                    ),
                }
            )
            return
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            if self.config.approval_policy == "never":
                decision = (
                    "approved_for_session"
                    if method in {"execCommandApproval", "applyPatchApproval"}
                    else "acceptForSession"
                )
                await self._send(session, {"id": request_id, "result": {"decision": decision}})
                await self.on_event({"event": "approval_auto_approved", "payload": message})
                return
            if self.approval_callback:
                outcome = await self.approval_callback(str(method), message)
                if outcome.get("approved"):
                    decision = (
                        "approved"
                        if method in {"execCommandApproval", "applyPatchApproval"}
                        else "accept"
                    )
                    await self._send(
                        session,
                        {"id": request_id, "result": {"decision": decision}},
                    )
                    await self.on_event({"event": "approval_approved", "payload": message})
                    return
                await self._send(
                    session,
                    {
                        "id": request_id,
                        "result": {
                            "decision": (
                                "denied"
                                if method in {"execCommandApproval", "applyPatchApproval"}
                                else "decline"
                            )
                        },
                    },
                )
                await self.on_event({"event": "approval_rejected", "payload": message})
                return
            await self._send(
                session,
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": "Operator approval unavailable"},
                },
            )
            raise CodexError("agent requested approval", category="approval_required")
        if method in {
            "item/tool/requestUserInput",
            "tool/requestUserInput",
            "mcpServer/elicitation/request",
        }:
            await self._send(
                session,
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": "Interactive input unavailable"},
                },
            )
            raise CodexError("agent requested user input", category="turn_input_required")
        await self._send(
            session,
            {"id": request_id, "error": {"code": -32601, "message": "Unsupported request"}},
        )

    async def _execute_project_validation(
        self,
        session: CodexSession,
        arguments: dict[str, Any],
        issue: Issue,
    ) -> dict[str, Any]:
        # A new attempt revokes prior evidence even if it fails or is interrupted.
        session.validation_fingerprint = None
        session.validation_head_sha = None
        session.validation_policy_digest = None
        self.tracker.revoke_publication(issue.id)
        is_review = getattr(session, "role", "implementation") == "review"
        if is_review:
            session.review_decision = None
            session.review_summary = None
            session.review_head_sha = None
            await self.on_event({"event": "review_invalidated", "attempt_id": uuid.uuid4().hex})
        head_before = await clean_workspace_head(session.workspace)
        if is_review and not head_before:
            return {
                "success": False,
                "output": "Commit all review changes before validating the clean review commit.",
                "contentItems": [],
            }
        fingerprint_before = await self._workspace_fingerprint(session.workspace)
        result = await self.validator.execute(arguments, session.workspace)
        fingerprint_after = await self._workspace_fingerprint(session.workspace)
        head_after = await clean_workspace_head(session.workspace)
        if fingerprint_before != fingerprint_after or (is_review and head_before != head_after):
            session.validation_fingerprint = None
            self.tracker.revoke_publication(issue.id)
            await self.on_event({"event": "validation_invalidated"})
            return {
                "success": False,
                "output": (
                    "Validation commands modified project files. Tempo rejected this validation "
                    "attempt. Make code changes through the normal workspace tools, then run "
                    "validation commands that leave project files unchanged."
                ),
                "contentItems": [],
            }
        if (
            result.get("success")
            and result.get("policy_digest") != self.validator.config.policy_digest
        ):
            await self.on_event({"event": "validation_invalidated"})
            return {"success": False, "output": "Validation policy changed; run validation again.",
                    "contentItems": []}
        if result.get("success"):
            session.validation_fingerprint = fingerprint_after
            session.validation_head_sha = head_after
            session.validation_policy_digest = result["policy_digest"]
            await self.on_event(
                {
                    "event": "validation_fingerprint_recorded",
                    "fingerprint": session.validation_fingerprint,
                    "head_sha": head_after,
                    "policy_digest": session.validation_policy_digest,
                }
            )
            self.tracker.authorize_publication(issue.id)
            self.tracker.accept_validation(
                fingerprint_after, policy_digest=session.validation_policy_digest,
            )
        return result

    @staticmethod
    def _completion_tool_spec() -> dict[str, Any]:
        return {
            "name": "tempo_complete",
            "description": (
                "Complete the ticket without a pull request only when the requested behavior is "
                "already present or no code change is required. Local validation must pass first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Concrete evidence explaining why no code change is needed.",
                    }
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        }

    def _tool_enabled(self, name: str) -> bool:
        return self.enabled_tools is None or name in self.enabled_tools

    @staticmethod
    def _review_tool_spec() -> dict[str, Any]:
        return {
            "name": "tempo_review",
            "description": (
                "Finish the independent pull-request review. Choose approve only after the current "
                "workspace passes unchanged local validation. Choose human_review when policy, "
                "ambiguity, permissions, sensitive changes, or unresolved risk require a person."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "human_review"],
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Concrete findings, evidence checked, and the reason for the decision."
                        ),
                    },
                },
                "required": ["decision", "summary"],
                "additionalProperties": False,
            },
        }

    @staticmethod
    async def _workspace_fingerprint(workspace: Path) -> str:
        return await workspace_fingerprint(workspace)

    async def stop_session(self, session: CodexSession) -> None:
        await stop_process_group(session.process)
        session.reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.reader_task

    async def _send(self, session: CodexSession, message: dict[str, Any]) -> None:
        if not session.process.stdin:
            raise CodexError("Codex stdin is unavailable", category="port_exit")
        session.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        try:
            await session.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexError("Codex process exited", category="port_exit") from exc

    async def _response(
        self, session: CodexSession, request_id: int, timeout_ms: int
    ) -> dict[str, Any]:
        while True:
            message = await self._next_message(session, timeout_ms)
            if message.get("id") != request_id:
                await self.on_event(
                    self._normalize_usage_for_attempt(session, self._event_from_message(message))
                )
                continue
            if "error" in message:
                raise CodexError(
                    f"Codex response error: {message['error']}", category="response_error"
                )
            return message.get("result") or {}

    async def _next_message(self, session: CodexSession, timeout_ms: int) -> dict[str, Any]:
        if session.process.returncode is not None and session.inbox.empty():
            raise CodexError(
                f"Codex exited with status {session.process.returncode}", category="port_exit"
            )
        try:
            message = await asyncio.wait_for(session.inbox.get(), timeout=timeout_ms / 1000)
        except TimeoutError as exc:
            raise CodexError("Codex response timed out", category="turn_timeout") from exc
        if message.get("_stream_closed"):
            raise CodexError("Codex stdout closed", category="port_exit")
        return message

    async def _read_stdout(
        self, process: asyncio.subprocess.Process, inbox: asyncio.Queue[dict[str, Any]]
    ) -> None:
        assert process.stdout
        while line := await process.stdout.readline():
            try:
                await inbox.put(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self.on_event(
                    {"event": "malformed", "payload": line.decode(errors="replace")[:1000]}
                )
        await inbox.put({"_stream_closed": True})

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr
        while line := await process.stderr.readline():
            await log.adebug("codex_stderr", message=line.decode(errors="replace").rstrip()[:1000])

    @staticmethod
    def _event_from_message(message: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event": message.get("method", "other_message"),
            "payload": message.get("params", message),
        }
        token_usage = CodexAppServer._find_key(message, "tokenUsage")
        usage = None
        if isinstance(token_usage, dict):
            cumulative = token_usage.get("total")
            usage = cumulative if isinstance(cumulative, dict) else token_usage
        if not isinstance(usage, dict) or not {
            "inputTokens",
            "outputTokens",
            "totalTokens",
        } & usage.keys():
            usage = CodexAppServer._find_mapping(
                message, {"inputTokens", "outputTokens", "totalTokens"}
            )
        if usage:
            event["usage"] = {
                "input_tokens": int(usage.get("inputTokens", 0) or 0),
                "output_tokens": int(usage.get("outputTokens", 0) or 0),
                "total_tokens": int(usage.get("totalTokens", 0) or 0),
            }
        limits = CodexAppServer._find_key(message, "rateLimits")
        if limits is not None:
            event["rate_limits"] = limits
        return event

    @staticmethod
    def _find_mapping(value: Any, keys: set[str]) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if keys & value.keys():
                return value
            for child in value.values():
                found = CodexAppServer._find_mapping(child, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = CodexAppServer._find_mapping(child, keys)
                if found:
                    return found
        return None

    @staticmethod
    def _find_key(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = CodexAppServer._find_key(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = CodexAppServer._find_key(child, key)
                if found is not None:
                    return found
        return None
