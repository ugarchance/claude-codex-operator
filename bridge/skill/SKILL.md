---
name: agent-request
description: Send a task to Claude Code or Codex on another (or this) machine through agent-bridge, follow it, read its result, continue the same session, or hand it to Claude Desktop / the Codex app. Use when work must run on a specific computer (its files, devices, accounts or apps), when the user asks to start or continue an agent session elsewhere, or when a job should report back to the dispatcher.
---

# Agent request (cross-machine)

`agent-bridge` runs Claude Code and Codex jobs on registered machines. You talk to it through `dispatch.py`, which reads the machine list from `machines.json` and forwards each command over SSH (or runs it locally). See `bridge/README.md` in the claude-codex-operator repository for setup.

Find the dispatcher first: `~/.local/share/agent-bridge/dispatch.py` (macOS/Linux) or `%LOCALAPPDATA%\agent-bridge\bin\dispatch.py` (Windows). `dispatch.py machines` lists the machines and, when the owner set one, a `routing` note (placement rules or the path of their routing notes): read it before you choose a machine. `dispatch.py status` shows each machine's CLIs, launcher, privacy runner and running jobs.

## Choose the machine and the folder

- Run work where its files, devices, signing keys or accounts are. Do not guess a project's path on another machine: check the user's routing notes, or list candidates with a read-only job, or ask.
- One project must not have two writers. Before starting work in a repository on a machine, make sure no other session is changing the same checkout.

## Start, follow, continue

```sh
# new session; the prompt goes on stdin (UTF-8), never on the remote command line
dispatch.py MACHINE start claude --cwd '~/path/to/project' --desktop --notify <<'EOF'
<self-contained task: goal, constraints, what "done" means, how to verify, what to report>
EOF
dispatch.py MACHINE start codex  --cwd '~/path/to/project' --desktop --notify <<'EOF'
...
EOF

dispatch.py MACHINE job wait JOB --timeout 1800     # blocks until done/failed (or the timeout)
dispatch.py MACHINE job result JOB                  # final text
dispatch.py MACHINE job list                        # recent jobs with session ids
dispatch.py MACHINE job cancel JOB                  # end the running agent; the job reports `cancelled`

dispatch.py MACHINE send claude --session SESSION_ID --notify <<'EOF'   # follow-up in the same Claude session
...
EOF
dispatch.py MACHINE send codex --session THREAD_ID --notify <<'EOF'     # runs inside the Codex app thread
...
EOF
dispatch.py MACHINE desktop claude --session SESSION_ID   # hand an existing session to Claude Desktop
```

- `start` returns `{"ok": true, "job": ...}` immediately. Delivery is not completion: wait for the job (or the `--notify` report) before saying the work is done, and quote the agent's own result.
- `--desktop` makes the session visible in Claude Desktop / the Codex app afterwards, so the user can continue it there. `--notify` sends the result to the dispatcher's return channel.
- `send claude` refuses while the session is open in another running Claude process (exit 3). Then start a new session that names the earlier one, or ask the user before `--force`.
- A job that prints nothing for a few minutes on a Mac is usually waiting on a macOS privacy prompt: `job show` then carries a `warning` (and `--notify` jobs report it). Ask the user to allow it on that Mac, or `job cancel` it.
- Options: `--model`, `--permission-mode` (`auto` by default; `bypassPermissions` only where that machine allows it), `--name` (Claude session title), `--timeout-h`.

## Writing the prompt

The remote agent knows nothing about this conversation. Give: the repository and branch to use, the goal, constraints (what not to touch, which environment), the definition of done, how to verify, and what to report. Reference files by path on that machine; do not paste secrets.

## From inside a job

A running job has `AGENT_BRIDGE_JOB` set. To ask the dispatcher a question mid-task, run `agent_bridge.py notify --prompt "..."` on the same machine; the answer arrives as a follow-up (`send`).

## Shell notes

- Git Bash on Windows rewrites `/Users/...` and `~` before a native program sees them: run `MSYS_NO_PATHCONV=1 python dispatch.py ...` and single-quote remote paths (`--cwd '~/src/app'`).
- Every command prints one JSON object. `{"ok": false, "error": ...}` means nothing was started; report the error instead of retrying blindly.
