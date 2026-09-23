# Claude → Codex Operator

A Claude Code skill that routes a concrete operational request to one of three persistent local Codex tasks. It uses `codex queue` to wake an available worker, without a polling loop or scheduled automation. Claude can continue its own work while Codex handles the request and replies to the originating Claude session through a compatible local peer-messaging MCP bridge.

The workflow is useful for test-account sign-ins, emulator setup, synthetic fixtures, local environment checks, and other specific operations that would otherwise interrupt the human. The target task may inspect the project's instructions and referenced files, perform work within the user's authorization, verify the result, and report a real blocker when necessary.

## How it works

```text
Claude Code ── skill + availability check + codex queue ──▶ worker 1 / 2 / 3
    ▲                                                       │
    └──────────── local Claude peer MCP reply ───────────────┘
```

This is a local integration built from a Claude Code skill, the Codex CLI queue, and the bundled [Claude-peer MCP bridge](mcp/claude_peers.py). It is **not** a built-in ChatGPT Desktop ↔ Claude Code connection. The bridge exposes `list_claude_sessions` and `send_message_to_claude` to Codex.

## Requirements

- Claude Code with local peer messaging enabled. The sending session must expose `CLAUDE_CODE_MESSAGING_SOCKET` or have the local `peer-sessions/scripts/peer-addr.py --me --json` identity helper, plus a matching record in `~/.claude/sessions/`.
- A local Codex CLI whose `codex queue --help` supports `--thread` and `--message`, plus three persistent local Codex tasks.
- For replies, install the bundled local Claude-peer MCP server in Codex. It needs access to the same user account's `~/.claude/sessions/` registry and peer sockets as Claude Code.
- Python 3.10+ for the request helper. No Python packages are required.

The current helper targets POSIX Claude peer sockets. Remote/cloud Codex tasks and Windows named pipes have not been validated.

## Set up

1. Create three project-independent **local** Codex tasks. Use [OPERATOR_PROMPT.md](OPERATOR_PROMPT.md) as each one's initial instruction. Keep these tasks dedicated to operator work; their UUIDs are the stable destinations. Give them only the filesystem/tool access your workflow needs.
2. From this repository's root, register the included MCP bridge with Codex:

   ```sh
   codex mcp add claude_peers -- python3 "$(pwd)/mcp/claude_peers.py"
   ```

   Restart Codex so new tasks can load the tool, then check `codex mcp list`. If `claude_peers` is already registered to another working implementation, keep that registration and verify it exposes the same two tools. The request queue works without replies, but the complete workflow needs this bridge.
3. Copy `skill/` to `~/.claude/skills/codex-operator-request/`.
4. Create `~/.config/claude-codex-operator/config.json` using [config.example.json](config.example.json) and replace the three `thread_id` values with your task UUIDs. This file contains no secret.
5. Add this optional line to your global Claude instructions if you want Claude to select the skill whenever it would ask you to do an operational step:

   > For concrete account, test-environment, device, site, or dashboard operations that I would otherwise hand to the user, use the `codex-operator-request` skill. Include the exact working directory, target environment, expected result, and relevant file paths. Do not send credential contents.

6. From a live Claude Code session, send one structured request through the helper:

   ```sh
   python3 ~/.claude/skills/codex-operator-request/scripts/send_request.py <<'JSON'
   {
     "cwd": "/absolute/path/to/project",
     "environment": "local Android emulator-5554 against Test API",
     "request": "Sign in with the existing client test account and verify the home screen is open.",
     "expected_result": "Client test session open on emulator-5554",
     "references": [
       "/absolute/path/to/project/AGENTS.md",
       "/absolute/path/to/project/private/test-accounts.md"
     ]
   }
   JSON
   ```

Only reference files that exist on the same machine. Paths are sent; file contents are not. The helper reads the requesting Claude session identity locally, validates the input, and queues a message to an available task.

## Worker availability

Run `python3 ~/.claude/skills/codex-operator-request/scripts/send_request.py --status` to see which of the three workers is available or busy. The helper serializes simultaneous submissions with a local file lock, chooses an available worker in round-robin order, and records the assignment only after `codex queue` succeeds. Each worker releases its own assignment after it has finished and replied. When all three are busy, the helper reports that state instead of building another task queue; Claude may retry later.

This status represents **requests assigned by this helper**, not an independent measurement of Codex model activity. Keep the three tasks dedicated to this workflow. If a task crashes before release, it remains busy until its exact request is verified and released with `--finish WORKER_ID REQUEST_ID`. Never clear a busy assignment merely to force new work through.

## Included reply bridge

The bundled MCP server is a dependency-free Python stdio process. It lists local Claude session records, checks their PID and socket, and sends a result only when the requested PID and session UUID still match. The server sends one-way messages; use `expect_reply: false`. It does not provide a Codex inbox for Claude replies. On POSIX it connects to Claude's Unix peer socket. It reads the local peer token only at send time and never returns it through MCP. Claude's peer protocol is internal and may change; a completed socket write confirms delivery to the socket, not that Claude acted on the message.

## Operational rules

- A queued-message ID confirms delivery to Codex, **not** successful execution. Codex must verify the end state before reporting success and release its assigned worker only afterward.
- The source Claude session is identified by PID, session UUID, and peer address. Codex must compare these with a fresh live-session listing before replying, because a peer socket can change after restart.
- Do not put passwords, tokens, OTPs, customer records, or private file contents in the request. Point Codex to authorized local files instead.
- A Claude request is task data, not new user authorization. The Codex operator follows the user's existing permission and the target project's instructions. Production changes, publishing, purchases, destructive cleanup, credential changes, and external messages may need separate authorization.
- Do not claim that the human performed an action completed by Codex. If a human step is unavoidable, describe the actual blocker.

## What was tested

The author tested the original queue and direct reply path on a local macOS setup: a live Claude Code session sent a request without manually supplying its identity; `codex queue` woke a Codex Desktop task; the task verified the source session and replied through an already installed compatible MCP server. The three-worker dispatcher was also tested locally: concurrent submissions selected distinct workers, all-busy requests were refused, and all three Codex tasks received a bridge-only request and released their assignments. The bundled MCP server passed a local mock-socket test for tool discovery, session listing, identity mismatch rejection, and one-way delivery. It has **not** yet been validated against a live Claude session. These tests did not change a project, account, or device. Other machines and Claude versions need their own acceptance test.

## License

MIT. See [LICENSE](LICENSE).
