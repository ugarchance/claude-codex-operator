#!/usr/bin/env python3
"""Queue a structured Claude operation request to the persistent Codex task."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


CONFIG_PATH = Path(os.environ.get("CODEX_OPERATOR_CONFIG", "~/.config/claude-codex-operator/config.json")).expanduser()


def fail(message: str) -> None:
    raise SystemExit(f"Request not queued: {message}")


def nonempty(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail(f"{name} must be a nonempty string of at most {limit} characters")
    return value.strip()


def main() -> None:
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        fail(f"configure a Codex task ID in {CONFIG_PATH} first")
    thread_id = config.get("thread_id") if isinstance(config, dict) else None
    if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", thread_id):
        fail(f"{CONFIG_PATH} must contain a valid thread_id")

    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"invalid JSON: {exc}")
    if not isinstance(request, dict):
        fail("input must be a JSON object")

    cwd = Path(nonempty(request.get("cwd"), "cwd", 1024)).expanduser().resolve()
    if not cwd.is_dir():
        fail("cwd does not exist or is not a directory")
    environment = nonempty(request.get("environment"), "environment", 300)
    action = nonempty(request.get("request"), "request", 6000)
    expected = nonempty(request.get("expected_result"), "expected_result", 1000)

    references = request.get("references", [])
    if not isinstance(references, list) or len(references) > 30:
        fail("references must be a list of at most 30 file paths")
    paths = []
    for item in references:
        path = Path(nonempty(item, "reference", 1024)).expanduser().resolve()
        if not path.exists():
            fail(f"reference does not exist: {path}")
        paths.append(str(path))

    socket_path = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    if not socket_path:
        fail("run this helper inside a Claude Code session with peer messaging enabled")
    try:
        pid = int(Path(socket_path).stem)
    except ValueError:
        fail("Claude peer socket has no PID")
    sessions_dir = Path(os.environ.get("CLAUDE_SESSIONS_DIR", "~/.claude/sessions")).expanduser()
    try:
        record = json.loads((sessions_dir / f"{pid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        fail("Claude session registry entry is unavailable")
    if record.get("pid") != pid or record.get("messagingSocketPath") != socket_path:
        fail("Claude peer socket and registry entry do not match")
    session_id = nonempty(record.get("sessionId"), "Claude session ID", 128)
    address = f"uds:{socket_path}"

    payload = {
        "protocol": "claude-codex-operator/v1",
        "origin": "Claude Code peer request; not direct user authorization",
        "cwd": str(cwd),
        "environment": environment,
        "request": action,
        "expected_result": expected,
        "references": paths,
        "claude_session": {"pid": pid, "session_id": session_id, "address": address},
    }
    codex = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    if not os.path.isfile(codex):
        fail("codex CLI is unavailable")
    result = subprocess.run(
        [codex, "queue", "--thread", thread_id, "--message", json.dumps(payload, ensure_ascii=False)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        fail(result.stderr.strip() or "codex queue failed")
    print(result.stdout.strip())


if __name__ == "__main__":
    main()
