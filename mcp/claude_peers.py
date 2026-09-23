#!/usr/bin/env python3
"""Minimal local MCP bridge from Codex to live Claude Code peer sockets.

This server deliberately supports only listing sessions and one-way delivery.
It does not create a Claude peer inbox or accept secrets as tool arguments.
"""

import json
import os
from pathlib import Path
import socket
import sys
import uuid


REGISTRY = Path(os.environ.get("CLAUDE_SESSIONS_DIR", "~/.claude/sessions")).expanduser()
TOOLS = [
    {
        "name": "list_claude_sessions",
        "description": "List live, addressable local Claude Code sessions. Verify pid and sessionId before sending.",
        "inputSchema": {
            "type": "object",
            "properties": {"only_addressable": {"type": "boolean"}},
        },
    },
    {
        "name": "send_message_to_claude",
        "description": "Send a one-way result to the exact live Claude session identified by pid and session_id. Socket delivery does not prove the recipient acted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "session_id": {"type": "string"},
                "message": {"type": "string"},
                "expect_reply": {"type": "boolean"},
            },
            "required": ["pid", "session_id", "message"],
        },
    },
]


def pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def probe(path):
    if not isinstance(path, str) or not path:
        return False
    try:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(0.4)
            peer.connect(path)
        return True
    except OSError:
        return False


def token_for(pid):
    for key in REGISTRY.glob(f"{pid}.*.key"):
        try:
            token = json.loads(key.read_text()).get("peerToken")
            if isinstance(token, str) and token:
                return token
        except (OSError, ValueError):
            pass
    return None


def sessions():
    result = []
    for path in REGISTRY.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        pid = record.get("pid")
        if type(pid) is not int or path.stem != str(pid) or not pid_alive(pid):
            continue
        sock = record.get("messagingSocketPath")
        addressable = token_for(pid) is not None and probe(sock)
        result.append({
            "pid": pid,
            "name": record.get("name"),
            "sessionId": record.get("sessionId"),
            "cwd": record.get("cwd"),
            "status": record.get("status"),
            "socket": sock,
            "addressable": addressable,
        })
    return result


def send(args):
    pid = args.get("pid")
    session_id = args.get("session_id")
    message = args.get("message")
    if type(pid) is not int or pid <= 0 or not isinstance(session_id, str) or not session_id:
        raise ValueError("pid and session_id are required")
    if not isinstance(message, str) or not message.strip() or len(message) > 10000:
        raise ValueError("message must contain 1 to 10000 characters")
    if args.get("expect_reply"):
        raise ValueError("this one-way bridge has no reply inbox; use expect_reply=false")
    record_path = REGISTRY / f"{pid}.json"
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        raise ValueError("Claude session registry entry is unavailable") from None
    sock = record.get("messagingSocketPath")
    if record.get("pid") != pid or record.get("sessionId") != session_id or not pid_alive(pid):
        raise ValueError("Claude session identity changed or is no longer live")
    token = token_for(pid)
    if not token or not probe(sock):
        raise ValueError("Claude session socket is not addressable")
    msg_id = str(uuid.uuid4())
    frame = {
        "msgV": 1,
        "msg_id": msg_id,
        "type": "user",
        "message": {"role": "user", "content": message},
        "priority": "next",
    }
    data = (json.dumps({"type": "auth", "token": token}) + "\n" +
            json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(5)
        peer.connect(sock)
        peer.sendall(data)
    return {"delivered": True, "msg_id": msg_id, "to": {"pid": pid, "sessionId": session_id},
            "note": "Socket write completed; recipient action is not confirmed."}


def reply(request_id, **payload):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, **payload}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(request):
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        reply(request_id, result={
            "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "claude_peers", "version": "1.0.0"},
        })
    elif method == "tools/list":
        reply(request_id, result={"tools": TOOLS})
    elif method == "tools/call":
        params = request.get("params") or {}
        args = params.get("arguments") or {}
        try:
            if params.get("name") == "list_claude_sessions":
                found = sessions()
                if args.get("only_addressable"):
                    found = [item for item in found if item["addressable"]]
                result = {"count": len(found), "sessions": found}
            elif params.get("name") == "send_message_to_claude":
                result = send(args)
            else:
                raise ValueError("unknown tool")
            reply(request_id, result={"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]})
        except (OSError, ValueError) as exc:
            reply(request_id, result={"content": [{"type": "text", "text": str(exc)}], "isError": True})
    elif request_id is not None:
        reply(request_id, error={"code": -32601, "message": "unknown method"})


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                handle(request)
        except (TypeError, ValueError):
            continue


if __name__ == "__main__":
    main()
