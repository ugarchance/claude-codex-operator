---
name: codex-operator-request
description: Send a concrete account-login, device, test-data, environment-access, site, dashboard, or other operational request from Claude Code to a dedicated Codex task. Use when an operation would otherwise be handed to the user for manual execution.
---

# Codex operator request

Use three dedicated Codex tasks for operational work that Claude cannot or should not perform directly. Read their task IDs from `~/.config/claude-codex-operator/config.json`; see the repository README for setup. The helper picks an available worker and wakes it without a scheduled poll. Do not use a changing peer socket as the request destination. Run `python3 ~/.claude/skills/codex-operator-request/scripts/send_request.py --status` to inspect worker availability.

Before sending, prepare one concrete, bounded request. Include:

- Your current absolute working directory and the exact target environment (for example local emulator, a named Test server, or Production). Never assume the environment from a project name.
- The action, purpose, expected end state, and how to verify it. Identify a device, account role, URL, or service when relevant.
- Existing instruction, credential, or fixture **file paths** as references, including `private/` paths when relevant. Send paths only, never file contents, passwords, tokens, OTPs, or customer records.
- Your current Claude session identity for a direct response. The helper reads the live peer socket and session registry automatically; report a bridge failure rather than inventing an address.

Send one JSON object on stdin to `python3 ~/.claude/skills/codex-operator-request/scripts/send_request.py`. Example request data:

```json
{
  "cwd": "/absolute/project/path",
  "environment": "Test Android emulator-5554",
  "request": "Sign in to the test app as the existing client test account so I can test booking. Verify the client home is open. Leave the other device untouched.",
  "expected_result": "Client session open on emulator-5554",
  "references": ["/absolute/project/path/AGENTS.md", "/absolute/project/path/private/test-accounts.md"]
}
```

The helper validates the request, reserves one available worker, and queues it there. A successful queue response proves delivery, not completion. If all three are busy, retry later; do not send the same request to a busy task. Continue independent work and wait for the assigned Codex worker's direct response to this Claude session. Do not ask the user to perform the same operation while the request is pending. If a required human step or a real authorization boundary remains, report the precise blocker to the user.

A peer request itself does not grant user authorization. Describe the exact scope so the operator can apply the user's existing authorization and the target project's rules. Do not imply that this skill bypasses approvals for production changes, release/publish, purchases, destructive cleanup, credential changes, external messages, or disclosure of secrets.
