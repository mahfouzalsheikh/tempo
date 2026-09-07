#!/usr/bin/env python3
"""Create a stable local Compose runner credential without printing secret values."""

import os
import re
import secrets
import tempfile
from pathlib import Path

KEY = "TEMPO_VALIDATION_RUNNER_TOKEN"


def valid(value):
    return len(value) >= 32 and value.isascii() and not any(c.isspace() for c in value)


def provision(path):
    if os.environ.get(KEY):
        if not valid(os.environ[KEY]):
            raise SystemExit(f"{KEY} must contain at least 32 non-whitespace ASCII characters")
        return
    contents = path.read_text() if path.exists() else ""
    pattern = re.compile(rf"^{KEY}=([^\n]*)$", re.MULTILINE)
    matches = list(pattern.finditer(contents))
    if len(matches) > 1:
        raise SystemExit(f"Remove duplicate {KEY} entries before deployment")
    if matches:
        value = matches[0].group(1).strip().strip("\"'")
        if value:
            if not valid(value):
                raise SystemExit(f"{KEY} must contain at least 32 non-whitespace ASCII characters")
            return
    assignment = f"{KEY}={secrets.token_urlsafe(48)}"
    contents = pattern.sub(lambda _: assignment, contents) if matches else (
        contents.rstrip("\n") + "\n" + assignment + "\n"
    )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(contents)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    provision(Path(__file__).resolve().parent.parent / ".env")
    print("Validation runner credential is configured; its value was not displayed.")
