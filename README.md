# Claude → Codex Operator

A Claude Code skill that routes a concrete operational request to one persistent local Codex task. It uses `codex queue` to wake the task when needed, without a polling loop or scheduled automation. Claude can continue its own work while Codex handles the request and replies to the originating Claude session through a compatible local peer-messaging MCP bridge.

The workflow is useful for test-account sign-ins, emulator setup, synthetic fixtures, local environment checks, and other specific operations that would otherwise interrupt the human. The target task may inspect the project's instructions and referenced files, perform work within the user's authorization, verify the result, and report a real blocker when necessary.

## How it works

```text
Claude Code ── skill + codex queue ──▶ persistent Codex task
    ▲                                      │
    └──── local Claude peer MCP reply ─────┘
```

This is a local integration built from a Claude Code skill, the Codex CLI queue, and a separate Claude-peer MCP bridge. It is **not** a built-in ChatGPT Desktop ↔ Claude Code connection, and the skill does not provide the reply MCP server itself. The exact setup demonstrated by the author used Codex Desktop and a local `claude_peers` MCP server exposing `list_claude_sessions` and `send_message_to_claude`.

## Requirements

- Claude Code with local peer messaging enabled. The sending session must expose `CLAUDE_CODE_MESSAGING_SOCKET` and a matching record in `~/.claude/sessions/`.
- A local Codex CLI whose `codex queue --help` supports `--thread` and `--message`, plus a persistent local Codex task.
- For replies, a compatible Codex MCP tool that can list live Claude sessions and send a message to the verified source session. This repo intentionally does not bundle an internal peer-socket transport.
- Python 3.10+ for the request helper. No Python packages are required.

The current helper targets POSIX Claude peer sockets. Remote/cloud Codex tasks and Windows named pipes have not been validated.

## Set up

1. Create a project-independent **local** Codex task. Use [OPERATOR_PROMPT.md](OPERATOR_PROMPT.md) as its initial instruction. Keep that task; its UUID is the stable destination. Give it only the filesystem/tool access your workflow needs.
2. Install a compatible local Claude-peer MCP bridge in Codex if you want direct replies to Claude. Check that Codex can list the source Claude session and message it. The request queue works independently of this reply channel.
3. Copy `skill/` to `~/.claude/skills/codex-operator-request/`.
4. Create `~/.config/claude-codex-operator/config.json` using [config.example.json](config.example.json) and replace `thread_id` with your task UUID. This file contains no secret.
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

Only reference files that exist on the same machine. Paths are sent; file contents are not. The helper reads the requesting Claude session identity locally, validates the input, and queues a message to the configured task.

## Operational rules

- A queued-message ID confirms delivery to Codex, **not** successful execution. Codex must verify the end state before reporting success.
- The source Claude session is identified by PID, session UUID, and peer address. Codex must compare these with a fresh live-session listing before replying, because a peer socket can change after restart.
- Do not put passwords, tokens, OTPs, customer records, or private file contents in the request. Point Codex to authorized local files instead.
- A Claude request is task data, not new user authorization. The Codex operator follows the user's existing permission and the target project's instructions. Production changes, publishing, purchases, destructive cleanup, credential changes, and external messages may need separate authorization.
- Do not claim that the human performed an action completed by Codex. If a human step is unavoidable, describe the actual blocker.

## What was tested

The author tested the queue and direct reply path on a local macOS setup: a live Claude Code session sent a request without manually supplying its identity; `codex queue` woke the fixed Codex Desktop task; the task verified the source session and replied. This test did not change a project, account, or device. Other machines and bridge implementations still need their own acceptance test.

## License

MIT. See [LICENSE](LICENSE).
