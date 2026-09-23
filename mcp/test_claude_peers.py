"""Behavior checks for the bundled one-way MCP bridge."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid


SERVER = Path(__file__).with_name("claude_peers.py")


class BridgeTest(unittest.TestCase):
    def test_lists_and_sends_only_to_matching_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sock_path = root / "peer.sock"
            received = []
            ready = threading.Event()

            def peer():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(sock_path))
                    server.listen()
                    ready.set()
                    for _ in range(3):  # list probe, send probe and actual delivery
                        conn, _ = server.accept()
                        with conn:
                            data = b""
                            while True:
                                chunk = conn.recv(65536)
                                if not chunk:
                                    break
                                data += chunk
                            if data:
                                received.extend(json.loads(line) for line in data.decode().splitlines())

            thread = threading.Thread(target=peer, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(2))
            pid = os.getpid()
            session_id = str(uuid.uuid4())
            (root / f"{pid}.json").write_text(json.dumps({
                "pid": pid, "sessionId": session_id, "name": "fixture",
                "messagingSocketPath": str(sock_path), "cwd": temporary,
            }))
            (root / f"{pid}.fixture.key").write_text(json.dumps({"peerToken": "test-only-token"}))
            calls = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "list_claude_sessions", "arguments": {"only_addressable": True}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "send_message_to_claude", "arguments": {
                     "pid": pid, "session_id": "wrong-session", "message": "must not send"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "send_message_to_claude", "arguments": {
                     "pid": pid, "session_id": session_id, "message": "verified result", "expect_reply": False}}},
            ]
            result = subprocess.run(
                [sys.executable, str(SERVER)],
                input="".join(json.dumps(call) + "\n" for call in calls),
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "CLAUDE_SESSIONS_DIR": temporary},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = {item["id"]: item for item in map(json.loads, result.stdout.splitlines())}
            self.assertEqual({tool["name"] for tool in replies[2]["result"]["tools"]},
                             {"list_claude_sessions", "send_message_to_claude"})
            listed = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertEqual(listed["sessions"][0]["sessionId"], session_id)
            self.assertTrue(replies[4]["result"]["isError"])
            self.assertNotIn("test-only-token", result.stdout)
            self.assertFalse(replies[5]["result"].get("isError", False))
            thread.join(2)
            self.assertEqual(len(received), 2)
            self.assertEqual(received[0]["token"], "test-only-token")
            self.assertEqual(received[1]["message"]["content"], "verified result")


if __name__ == "__main__":
    unittest.main()
