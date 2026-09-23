#!/usr/bin/env python3
"""Route Claude requests to three dedicated Codex operator tasks."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


CONFIG_PATH = Path(os.environ.get("CODEX_OPERATOR_CONFIG", "~/.config/claude-codex-operator/config.json")).expanduser()
STATE_PATH = CONFIG_PATH.with_name("dispatch-state.json")
LOCK_PATH = CONFIG_PATH.with_name("dispatch.lock")
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def fail(message):
    raise SystemExit(f"Request not queued: {message}")


def nonempty(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail(f"{name} must be a nonempty string of at most {limit} characters")
    return value.strip()


def workers():
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        fail(f"configure three Codex task IDs in {CONFIG_PATH} first")
    entries = config.get("workers") if isinstance(config, dict) else None
    if not isinstance(entries, list) or len(entries) != 3:
        fail(f"{CONFIG_PATH} must contain exactly three workers")
    names, ids = set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail("each worker must have an id and thread_id")
        name = nonempty(entry.get("id"), "worker id", 40)
        thread_id = nonempty(entry.get("thread_id"), "thread_id", 36)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or not UUID_PATTERN.fullmatch(thread_id):
            fail("worker IDs must be simple names and thread IDs must be UUIDs")
        if name in names or thread_id in ids:
            fail("worker IDs and thread IDs must be unique")
        names.add(name)
        ids.add(thread_id)
    return entries


def load_state():
    try:
        value = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return {"assignments": {}, "next_index": 0}
    except (OSError, json.JSONDecodeError):
        fail(f"dispatch state cannot be read: {STATE_PATH}")
    if not isinstance(value, dict) or not isinstance(value.get("assignments"), dict) or not isinstance(value.get("next_index"), int):
        fail("dispatch state is invalid")
    return value


def save_state(value):
    temporary = STATE_PATH.with_name(f".{STATE_PATH.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def lock():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def status(entries):
    with lock():
        assignments = load_state()["assignments"]
        print(json.dumps({"workers": [
            {"id": entry["id"], "state": "busy" if entry["id"] in assignments else "available",
             "request_id": assignments.get(entry["id"], {}).get("request_id")}
            for entry in entries
        ]}, ensure_ascii=False))


def finish(entries, worker_id, request_id):
    if worker_id not in {entry["id"] for entry in entries} or not UUID_PATTERN.fullmatch(request_id):
        fail("invalid worker ID or request ID")
    with lock():
        value = load_state()
        assignment = value["assignments"].get(worker_id)
        if not assignment or assignment.get("request_id") != request_id:
            fail("worker assignment does not match; nothing released")
        del value["assignments"][worker_id]
        save_state(value)
    print(json.dumps({"worker": worker_id, "state": "available", "request_id": request_id}))


def request_payload():
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
        peer_helper = Path.home() / ".claude/skills/peer-sessions/scripts/peer-addr.py"
        if not peer_helper.is_file():
            fail("run inside a Claude Code session with peer messaging enabled")
        identity = subprocess.run(
            [sys.executable, str(peer_helper), "--me", "--json"],
            capture_output=True, text=True, check=False,
        )
        if identity.returncode:
            fail("this Claude session has no live peer address")
        try:
            details = json.loads(identity.stdout)
        except json.JSONDecodeError:
            fail("Claude peer identity helper returned invalid JSON")
        address = details.get("address")
        if not isinstance(address, str) or not address.startswith("uds:"):
            fail("this Claude session has no live peer address")
        socket_path = address[4:]
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

    return {
        "protocol": "claude-codex-operator/v2",
        "origin": "Claude Code peer request; not direct user authorization",
        "cwd": str(cwd), "environment": environment, "request": action,
        "expected_result": expected, "references": paths,
        "claude_session": {"pid": pid, "session_id": session_id, "address": f"uds:{socket_path}"},
    }


def dispatch(entries):
    payload = request_payload()
    codex = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    if not os.path.isfile(codex):
        fail("codex CLI is unavailable")
    with lock():
        value = load_state()
        start = value["next_index"] % len(entries)
        available = next((i for i in ((start + offset) % len(entries) for offset in range(len(entries)))
                          if entries[i]["id"] not in value["assignments"]), None)
        if available is None:
            fail("all three workers are busy; retry when one finishes")
        worker = entries[available]
        request_id = str(uuid.uuid4())
        payload.update({"request_id": request_id, "worker_id": worker["id"]})
        result = subprocess.run(
            [codex, "queue", "--thread", worker["thread_id"], "--message", json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            fail(result.stderr.strip() or "codex queue failed")
        value["assignments"][worker["id"]] = {"request_id": request_id}
        value["next_index"] = (available + 1) % len(entries)
        save_state(value)
    print(json.dumps({"worker": worker["id"], "request_id": request_id,
                      "queued": result.stdout.strip()}, ensure_ascii=False))


def main():
    entries = workers()
    if sys.argv[1:] == ["--status"]:
        status(entries)
    elif len(sys.argv) == 4 and sys.argv[1] == "--finish":
        finish(entries, sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 1:
        dispatch(entries)
    else:
        fail("usage: send_request.py [--status | --finish WORKER_ID REQUEST_ID]")


if __name__ == "__main__":
    main()
