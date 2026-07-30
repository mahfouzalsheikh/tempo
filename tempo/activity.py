from __future__ import annotations

from typing import Any


def _find_text(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        lowered = {str(key).lower(): child for key, child in value.items()}
        for key in keys:
            child = lowered.get(key.lower())
            if isinstance(child, str) and child.strip():
                return child.strip()
            if isinstance(child, list):
                text = _text_from_content(child)
                if text:
                    return text
        for child in value.values():
            found = _find_text(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        return _text_from_content(value)
    return ""


def _text_from_content(value: list[Any]) -> str:
    pieces: list[str] = []
    for child in value:
        if isinstance(child, str):
            pieces.append(child)
        elif isinstance(child, dict):
            text = _find_text(child, ("text", "delta", "content", "message"))
            if text:
                pieces.append(text)
    return "\n".join(pieces).strip()


def _find_raw_delta(value: Any) -> str:
    if isinstance(value, dict):
        delta = value.get("delta")
        if isinstance(delta, str):
            return delta
        for child in value.values():
            found = _find_raw_delta(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_raw_delta(child)
            if found:
                return found
    return ""


def activity_from_event(event: dict[str, Any], timestamp: str) -> dict[str, Any] | None:
    name = str(event.get("event", "unknown"))
    lowered = name.lower()
    payload = event.get("payload", event)

    if "tokenusage" in lowered or "ratelimit" in lowered:
        return None
    if "reasoning" in lowered:
        return _activity(name, timestamp, "thinking", "Agent is reasoning")
    if name == "session_started":
        return _activity(name, timestamp, "system", "Agent session started")
    if name == "validation_started":
        return _activity(
            name,
            timestamp,
            "validation",
            "Local validation started",
            str(event.get("summary", "")),
        )
    if name == "validation_command_started":
        return _activity(
            name,
            timestamp,
            "command",
            str(event.get("name", "Validation command")),
            str(event.get("command", "")),
        )
    if name == "validation_command_output_delta":
        return _activity(
            name,
            timestamp,
            "command",
            f"{event.get('name', 'Validation command')} output",
            str(event.get("delta", "")),
            coalesce=True,
        )
    if name == "validation_command_completed":
        exit_code = event.get("exit_code")
        return _activity(
            name,
            timestamp,
            "success" if exit_code == 0 else "error",
            f"{event.get('name', 'Validation command')} exited {exit_code}",
            str(event.get("output", ""))[-4000:],
        )
    if name == "validation_completed":
        success = bool(event.get("success"))
        return _activity(
            name,
            timestamp,
            "success" if success else "error",
            f"Local validation {'passed' if success else 'failed'}",
        )
    if name == "validation_invalidated":
        return _activity(
            name,
            timestamp,
            "warning",
            "Validation invalidated",
            "Project files changed after the successful validation run.",
        )
    if name == "no_change_completed":
        return _activity(
            name,
            timestamp,
            "success",
            "Completed without code changes",
            str(event.get("reason", "")),
        )
    if name == "review_completed":
        decision = str(event.get("decision", "human_review"))
        return _activity(
            name,
            timestamp,
            "success" if decision == "approve" else "warning",
            "Independent review approved" if decision == "approve" else "Human review requested",
            str(event.get("summary", "")),
        )
    if name == "review_outcome":
        status = str(event.get("status", "human_review"))
        return _activity(
            name,
            timestamp,
            "success" if status == "merged" else "warning",
            "Pull request merged" if status == "merged" else "Human review required",
            str(event.get("reason") or event.get("summary") or ""),
        )
    if name == "tool_call_completed":
        tool = str(event.get("tool", "tool"))
        arguments = event.get("arguments") or {}
        if tool == "github_api":
            detail = f"{str(arguments.get('method', 'GET')).upper()} {arguments.get('path', '')}"
        else:
            detail = _find_text(arguments, ("summary", "command", "path"))
        return _activity(
            name,
            timestamp,
            "tool" if event.get("success") else "error",
            f"{tool} {'completed' if event.get('success') else 'failed'}",
            detail,
        )
    if "agentmessage" in lowered or "assistantmessage" in lowered:
        text = _find_raw_delta(payload) or _find_text(payload, ("text", "content", "message"))
        if text:
            return _activity(
                name,
                timestamp,
                "message",
                "Agent update",
                text,
                coalesce="delta" in lowered,
            )
    if "commandexecution" in lowered or "execcommand" in lowered:
        command = _find_text(payload, ("command", "cmd"))
        output = _find_raw_delta(payload) or _find_text(
            payload, ("aggregatedoutput", "output", "text")
        )
        title = "Command running"
        kind = "command"
        if "completed" in lowered:
            title = "Command completed"
            kind = "success"
        elif "failed" in lowered:
            title = "Command failed"
            kind = "error"
        return _activity(
            name,
            timestamp,
            kind,
            title,
            "\n".join(part for part in (command, output) if part),
            coalesce="delta" in lowered,
        )
    if "filechange" in lowered or "applypatch" in lowered:
        path = _find_text(payload, ("path", "file", "filename"))
        return _activity(name, timestamp, "file", "Files updated", path)
    if "failed" in lowered or "error" in lowered:
        text = _find_text(payload, ("message", "error", "text", "detail"))
        return _activity(name, timestamp, "error", "Agent error", text)
    if lowered in {"turn/completed", "turn/failed", "turn/cancelled"}:
        return _activity(
            name,
            timestamp,
            "success" if lowered == "turn/completed" else "error",
            name.replace("/", " ").title(),
        )
    if "approval" in lowered:
        return _activity(name, timestamp, "warning", "Approval handled")

    item_type = _find_text(payload, ("type",))
    if item_type and "reasoning" in item_type.lower():
        return _activity(name, timestamp, "thinking", "Agent is reasoning")
    return _activity(name, timestamp, "system", name.replace("/", " · "))


def _activity(
    event: str,
    timestamp: str,
    kind: str,
    title: str,
    text: str = "",
    *,
    coalesce: bool = False,
) -> dict[str, Any]:
    return {
        "event": event,
        "at": timestamp,
        "kind": kind,
        "title": title,
        "text": text[:4000],
        "coalesce": coalesce,
    }


def append_activity(items: list[dict[str, Any]], activity: dict[str, Any] | None) -> None:
    if not activity:
        return
    if (
        activity.get("coalesce")
        and items
        and items[-1].get("event") == activity.get("event")
        and items[-1].get("kind") == activity.get("kind")
    ):
        items[-1]["text"] = f"{items[-1].get('text', '')}{activity.get('text', '')}"[-4000:]
        items[-1]["at"] = activity["at"]
    else:
        items.append(activity)
    items[:] = items[-60:]
