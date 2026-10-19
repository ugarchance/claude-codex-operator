# agent-bridge

Start and continue **Claude Code** and **Codex** sessions on any of your machines from one dispatcher, for example a chat agent such as Hermes that you message from your phone, or a Claude/Codex session on another computer. Sessions it starts can be handed to the desktop apps (Claude Desktop, the Codex app) so you can continue them there, and every result, or a question from the agent, can be sent back to the dispatcher.

```text
dispatcher ── dispatch.py ──ssh──▶ agent_bridge.py start|send ─▶ job ─▶ claude -p / codex exec / codex queue
     ▲                                                              │
     │                                                              ├─▶ --desktop: Claude Desktop / Codex app
     └──────────────── notify_command (JSON on stdin) ◀─────────────┘
```

- `agent_bridge.py` runs on every machine that does the work. Each request becomes a **job** with its own folder (`prompt.txt`, `status.json`, `output.jsonl`, `result.md`). The command returns the job id at once; the agent runs detached inside the logged-in desktop session.
- `dispatch.py` runs on the dispatcher. It knows the machines (`machines.json`) and forwards bridge commands over SSH. Prompts always travel on stdin as UTF-8, never on a remote command line.

Python 3.9+ and the standard library only. Tested on macOS (Python 3.9 and 3.14, direct and launchd launchers) and Windows 11 (Python 3.13, direct and Task Scheduler launchers).

## Commands

```sh
agent_bridge.py status                                   # CLIs, versions, launcher, live jobs
agent_bridge.py start claude|codex --cwd DIR [--name TITLE] [--model M] [--permission-mode MODE] \
                [--desktop] [--notify] [--timeout-h H] [--prompt TEXT | --prompt-file FILE | stdin]
agent_bridge.py send  claude|codex --session ID [--cwd DIR] [--force] [...same options]
agent_bridge.py desktop claude|codex --session ID       # hand a session to Claude Desktop / open a Codex thread
agent_bridge.py job list | show JOB | result JOB | wait JOB [--timeout S] | cancel JOB
agent_bridge.py notify [--job JOB] --prompt TEXT          # from inside a job: ask the dispatcher something
```

- `start claude`: `claude -p --output-format stream-json` in `--cwd`. `--desktop` then hands the session to Claude Desktop (`claude --desktop --resume ID`), so the conversation appears there and you can continue it in the app.
- `start codex`: `codex exec --json`. `--desktop` opens the new thread in the Codex app (`codex://threads/ID`).
- `send codex`: queues the message into an existing Codex thread (`codex queue`), wakes the app on that thread and waits for that turn's `task_complete` in the rollout. The turn runs in the Codex app, visible there.
- `send claude`: `claude -p --resume ID`. It refuses (exit 3) while that session is open in a running Claude process, because two writers would fork the transcript; start a new session that references it, or pass `--force`.
- `--notify` sends the result through `notify_command` when the job ends. An agent can call `agent_bridge.py notify` during a job (`AGENT_BRIDGE_JOB` is set in its environment) to ask a question.
- `job cancel` ends the job's running agent (or keeps it from starting) and waits for the worker to report the job as `cancelled`. A turn already queued into the Codex app keeps running there; only the wait stops.

Every command prints one JSON object (UTF-8). Errors: `{"ok": false, "error": "..."}` with exit code 2.

## Configuration

`~/.config/agent-bridge/config.json` (Windows: `%APPDATA%\agent-bridge\config.json`; override with `AGENT_BRIDGE_CONFIG`):

| key | meaning |
| --- | --- |
| `machine` | name used in reports (default: host name) |
| `language` | `en` or `tr` for report wording |
| `claude`, `codex` | CLI path or argv list (default: known install locations, then `PATH`) |
| `codex_model` | `-m` for `codex exec` when the request has no `--model` |
| `claude_permission_mode` | default `auto`; allowed: `default`, `acceptEdits`, `auto`, `plan`, `dontAsk` |
| `allow_bypass_permissions` | `true` additionally allows `--permission-mode bypassPermissions` on this machine |
| `notify_command` | argv list; receives `{"message", "kind", "job", "machine", "harness", "session", "state", "cwd"}` as JSON on stdin |
| `notify_max_chars` | result text limit in a report (default 1500) |
| `path` | `PATH` for agents (default: the login shell's `PATH`) |
| `mac_runner` | macOS: path of the privacy runner (default: `agent-bridge-runner` next to the script); `false` turns it off |
| `mac_runner_mode` | `auto` (default): use the runner while it holds Full Disk Access; `always`: whenever it is installed (for grants given through the folder prompts) |
| `stall_warning_s` | macOS: warn once when an agent has printed nothing for this many seconds (default 180, `0` turns it off) |

Jobs live in `~/.local/state/agent-bridge/jobs` (Windows: `%LOCALAPPDATA%\agent-bridge\jobs`; override with `AGENT_BRIDGE_STATE`).

Dispatcher `machines.json` (`~/.config/agent-bridge/machines.json`, Windows `%APPDATA%\agent-bridge\machines.json`, or `AGENT_BRIDGE_MACHINES`):

```json
{"routing": "Mobile releases on the laptop; anything new: ask first. Notes: ~/notes/routing.md",
 "machines": {
  "laptop":  {"ssh": "laptop", "python": "/usr/bin/python3", "bridge": "~/.local/share/agent-bridge/agent_bridge.py"},
  "desktop": {"local": true, "python": "C:/Python313/python.exe",
              "bridge": "C:/Users/me/AppData/Local/agent-bridge/bin/agent_bridge.py"}
}}
```

```sh
dispatch.py status                       # every machine
dispatch.py laptop start claude --cwd '~/src/app' --desktop --notify <<'EOF'
Fix the failing login test and summarize what changed.
EOF
dispatch.py laptop job wait 20260101-120000-ab12 --timeout 3600
```

`routing` is optional free text that `dispatch.py machines` prints with the machine list, so an agent reads the owner's placement rules before it picks a machine.

From Git Bash (for example an agent's terminal on Windows), prefix with `MSYS_NO_PATHCONV=1` and quote remote paths; otherwise Git Bash rewrites `/Users/...` into a Windows path. `dispatch.py` refuses such rewritten arguments.

## Install

```sh
./install.sh --machine laptop --language en                    # macOS / Linux
powershell -ExecutionPolicy Bypass -File install.ps1 -Machine desktop   # Windows
```

Then add `notify_command` (and anything else) to the config and run `agent_bridge.py status`.

On machines whose agents should dispatch work, install the skill: copy `bridge/skill/` to `~/.claude/skills/agent-request/` and `~/.codex/skills/agent-request/` (or keep one copy and link both).

## macOS notes

- **Launchers.** The worker runs as a transient launchd job in `gui/<uid>` so the login keychain (Claude sign-in, code signing) and `open` work even when the request arrives over SSH. The agent CLI itself is started as its own launchd job.
- **Privacy controls.** macOS judges folder access (Desktop, Documents, Downloads, Full Disk Access) by the *responsible* process. Because launchd starts the agent CLI itself, the prompts and grants name `claude` or `codex`, exactly as when you run them yourself. Install the scripts outside protected folders (`install.sh` uses `~/.local/share`).
- **The privacy runner.** A grant for a command-line tool belongs to its path, and Claude Code moves to a new versioned path with every update, so a grant given to `claude` lapses with the next update. `install.sh` therefore builds `agent-bridge-runner` from `runner.c` (ad-hoc signed; rebuilt only when the source changes, because a new binary loses its grant). Add `~/.local/share/agent-bridge/agent-bridge-runner` once under System Settings → Privacy & Security → Full Disk Access. While the runner holds that access (checked once per job, shown by `status`), launchd starts the runner and the agent runs as its child under the runner's grant. Without it the agent runs on its own and needs its own grant: answer the "would like to access files in your Desktop folder" prompt on that Mac, or add the CLI under Full Disk Access.
- **Stalled jobs.** A job blocked on a privacy prompt prints nothing. When an agent has printed nothing for `stall_warning_s` (default 180 seconds) since it started, the bridge records a `warning` in the job status and reports it once through `notify_command` for `--notify` jobs.
- **SSH logins** see protected folders only with "Allow full disk access for remote users" (System Settings → General → Sharing → Remote Login). The bridge does not need it for the agent run, but `--cwd` checks and repository syncs over SSH do.
- `--desktop` for Claude needs a terminal; the bridge gives the launchd job a pseudo-terminal.

## Windows notes

- **Launcher.** The worker runs through a one-shot Task Scheduler task with the user's interactive token (`agent-bridge\<job>`, removed after the job), so it lands in the desktop session even from an SSH login, where desktop apps could not be opened. It needs someone logged on to the desktop; if the task does not start within 20 seconds the bridge falls back to a detached process.
- Console tools run without windows. The bridge uses the native `codex.exe` (Codex app or npm vendor folder), never the npm `.cmd`/`.ps1` shims, because `cmd.exe` would re-parse the message.
- Config files written by Windows PowerShell 5 carry a BOM; the bridge accepts it.

## Security

- The bridge runs whatever prompt it receives with your accounts. Only expose it to dispatchers you trust (SSH keys, a firewall that limits who can reach SSH).
- Session ids are validated (`[0-9A-Za-z_-]`, 8–80 characters) before they reach a command line; Windows `cmd /c start` gets them as separate arguments.
- `bypassPermissions` is refused unless the machine's own config opts in.
- Job folders contain the prompt and the agent's output. They are created with your user's default permissions.

## Tests

```sh
python3 tests/test_agent_bridge.py                                 # fake CLIs, direct launcher
AGENT_BRIDGE_TEST_LAUNCHER=launchd python3 tests/test_agent_bridge.py   # macOS, logged-in user
AGENT_BRIDGE_TEST_LAUNCHER=task python tests\test_agent_bridge.py       # Windows, desktop logon
```

The tests use fake `claude`/`codex` executables (`tests/fakes.py`); they make no model calls and open no apps. On a Mac with the developer tools they also build the runner into a temporary folder and test it.
