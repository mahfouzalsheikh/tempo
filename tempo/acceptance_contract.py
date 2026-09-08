"""Versioned, declarative browser journeys. No project code or shell commands."""

import hashlib
import json
import shlex
from urllib.parse import urlsplit

ROLES = {"button", "link", "heading", "textbox", "checkbox", "radio", "tab", "status"}
RUNNER = "browser-acceptance-v1"
RUNNERS = {1: RUNNER, 2: "browser-acceptance-v2"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parse_steps(source, schema=1):
    if type(schema) is not int or schema not in RUNNERS:
        raise ValueError("Unsupported browser check version.")
    if not isinstance(source, str) or len(source) > 8000:
        raise ValueError("Check instructions must be at most 8,000 characters.")
    steps = []
    for number, line in enumerate(source.splitlines(), 1):
        if not line.strip():
            continue
        parts = shlex.split(line)
        valid = False
        if len(parts) == 2 and parts[0] == "open":
            path = urlsplit(parts[1])
            valid = (
                parts[1].startswith("/")
                and not parts[1].startswith("//")
                and not path.scheme
                and not path.netloc
                and "\\" not in parts[1]
            )
        elif len(parts) == 3:
            action, target, value = parts
            valid = (
                (action == "fill")
                or (
                    action == "click"
                    and target in ROLES | {"testid"} | ({"text"} if schema == 2 else set())
                )
                or (action == "expect" and target in ROLES | {"text", "testid"})
            )
        elif len(parts) == 4 and parts[:2] == ["expect", "testid"]:
            valid = True
        elif schema == 2 and len(parts) == 4:
            action, target, _name, contract = parts
            valid = (
                (
                    action == "upload"
                    and target in {"label", "testid"}
                    and contract == "png-circle-v1"
                )
                or (
                    action == "download"
                    and target in {"button", "link", "testid"}
                    and contract == "svg-v1"
                )
                or (
                    action == "press"
                    and target == "slider"
                    and contract
                    in {"Home", "End", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}
                )
            )
        if not valid or any(not p or len(p) > 1000 or any(ord(c) < 32 for c in p) for p in parts):
            raise ValueError(f"Line {number}: use one of the supported browser actions.")
        steps.append(parts)
    if not 1 <= len(steps) <= 20 or not any(step[0] == "expect" for step in steps):
        raise ValueError("Each criterion needs 1–20 steps, including an expect assertion.")
    return steps


def specification(brief_digest, criteria, instructions, schema=None):
    if schema is None:
        schema = (
            2
            if any(
                line.split()
                and (
                    line.split()[0] in {"upload", "download", "press"}
                    or line.split()[:2] == ["click", "text"]
                )
                for source in instructions.values()
                if isinstance(source, str)
                for line in source.splitlines()
            )
            else 1
        )
    if type(schema) is not int or schema not in RUNNERS:
        raise ValueError("Unsupported browser check version.")
    expected = [criterion["id"] for criterion in criteria]
    if set(instructions) != set(expected):
        raise ValueError("Provide checks for every criterion in this saved brief.")
    checks = [
        {
            "id": key,
            "instructions": instructions[key],
            "steps": parse_steps(instructions[key], schema),
        }
        for key in expected
    ]
    if sum(len(check["steps"]) for check in checks) > 100:
        raise ValueError("A check plan can contain at most 100 steps.")
    return {
        "schema": schema,
        "runner": RUNNERS[schema],
        "brief_digest": brief_digest,
        "checks": checks,
    }


def verify_specification(saved, expected_digest, brief_digest, criteria):
    rebuilt = specification(
        brief_digest,
        criteria,
        {check["id"]: check["instructions"] for check in saved["checks"]},
        schema=saved["schema"],
    )
    if saved != rebuilt or digest(saved) != expected_digest:
        raise ValueError("Saved acceptance plan failed its integrity check.")
    return rebuilt


def verify_report(report, suite, artifact_digest):
    if set(report) != {"schema", "runner", "suite_digest", "artifact_digest", "browser", "results"}:
        raise ValueError("Invalid browser report")
    if (
        type(report["schema"]) is not int
        or report["schema"] != suite["schema"]
        or report["runner"] != RUNNERS.get(suite["schema"])
        or report["suite_digest"] != digest(suite)
        or report["artifact_digest"] != artifact_digest
        or not isinstance(report["browser"], str)
        or not report["browser"]
    ):
        raise ValueError("Browser report identity mismatch")
    if len(report["results"]) != len(suite["checks"]):
        raise ValueError("Incomplete criterion evidence")
    passed = True
    for result, check in zip(report["results"], suite["checks"], strict=True):
        if set(result) != {"id", "status", "steps", "error"} or result["id"] != check["id"]:
            raise ValueError("Unexpected criterion evidence")
        if result["status"] not in {"passed", "failed"} or len(result["steps"]) > len(
            check["steps"]
        ):
            raise ValueError("Invalid criterion result")
        for index, step in enumerate(result["steps"]):
            action = check["steps"][index]
            file_step = suite["schema"] == 2 and action[0] in {"upload", "download"}
            expected_keys = {"action", "status"}
            if file_step and step.get("status") == "passed":
                expected_keys.add("file")
            if set(step) != expected_keys or step["action"] != action:
                raise ValueError("Browser steps do not match reviewed checks")
            if step["status"] not in {"passed", "failed"}:
                raise ValueError("Invalid browser step status")
            if file_step and step["status"] == "passed":
                from tempo.acceptance_files import verify_file_evidence

                verify_file_evidence(action, step["file"])
        if result["status"] == "passed":
            if (
                len(result["steps"]) != len(check["steps"])
                or result["error"]
                or any(step["status"] != "passed" for step in result["steps"])
            ):
                raise ValueError("A passing criterion requires every planned step")
        else:
            passed = False
    return passed
