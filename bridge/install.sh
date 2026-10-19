#!/bin/sh
# Install agent-bridge for the current user on macOS or Linux.
#   ./install.sh [--machine NAME] [--language en|tr] [--python /usr/bin/python3]
# Copies the scripts to ~/.local/share/agent-bridge (outside the folders that macOS privacy controls guard, so the
# launchd worker can read them), writes a starter config if there is none, and prints `status`.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
machine=$(hostname -s | tr '[:upper:]' '[:lower:]')
language=en
python=
while [ $# -gt 0 ]; do
  case "$1" in
    --machine) machine=$2; shift 2 ;;
    --language) language=$2; shift 2 ;;
    --python) python=$2; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$python" ]; then
  for candidate in /usr/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
      python=$(command -v "$candidate"); break
    fi
  done
fi
[ -n "$python" ] || { echo "Python 3.9+ not found" >&2; exit 1; }
dest="${AGENT_BRIDGE_HOME:-$HOME/.local/share/agent-bridge}"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/agent-bridge"
mkdir -p "$dest" "$conf"
cp "$here/agent_bridge.py" "$here/dispatch.py" "$dest/"
chmod 755 "$dest/agent_bridge.py" "$dest/dispatch.py"
if [ "$(uname -s)" = Darwin ]; then
  # the privacy runner is rebuilt only when its source changes: a new binary loses its Full Disk Access grant
  runner="$dest/agent-bridge-runner"
  if [ -x "$runner" ] && cmp -s "$here/runner.c" "$dest/runner.c"; then
    echo "runner unchanged: $runner"
  elif xcode-select -p >/dev/null 2>&1 && cc -O2 -Wall -o "$runner.new" "$here/runner.c" -framework Security -framework CoreFoundation \
      && codesign --force --sign - --options runtime --identifier agent-bridge-runner "$runner.new" 2>/dev/null; then
    mv -f "$runner.new" "$runner"
    cp "$here/runner.c" "$dest/runner.c"
    echo "built $runner"
    echo "  add it once: System Settings > Privacy & Security > Full Disk Access > + (Cmd-Shift-G, paste the path)"
  else
    rm -f "$runner.new"
    echo "runner not built (needs the developer tools: xcode-select --install); agents run without it" >&2
  fi
fi
if [ ! -f "$conf/config.json" ]; then
  "$python" - "$conf/config.json" "$machine" "$language" <<'PY'
import json, sys
path, machine, language = sys.argv[1:]
with open(path, 'w', encoding='utf-8') as handle:
    json.dump({'machine': machine, 'language': language}, handle, indent=1)
    handle.write('\n')
PY
  echo "wrote $conf/config.json (add notify_command, codex_model, ... as needed)"
fi
echo "installed: $python $dest/agent_bridge.py"
"$python" "$dest/agent_bridge.py" status
