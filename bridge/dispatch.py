#!/usr/bin/env python3
"""dispatch: run agent-bridge commands on any registered machine (over SSH, or locally).

usage:
  dispatch.py machines                         list the registered machines
  dispatch.py status [MACHINE ...]             `agent_bridge.py status` on the given (default: all) machines
  dispatch.py MACHINE BRIDGE-ARGS...           forward any agent_bridge.py command to MACHINE

The prompt never travels on a remote command line: `--prompt TEXT`, `--prompt-file LOCAL_FILE` or text on stdin is
sent to the bridge through stdin as UTF-8, so quoting, length and code pages cannot damage it.

Machines live in machines.json (AGENT_BRIDGE_MACHINES, else %APPDATA%\\agent-bridge\\machines.json or
~/.config/agent-bridge/machines.json):

  {"routing": "which project runs where: a short rule or the path of the owner's routing notes",
   "machines": {
     "laptop": {"ssh": "laptop", "python": "/usr/bin/python3", "bridge": "~/.local/share/agent-bridge/agent_bridge.py"},
     "here":   {"local": true, "python": "C:/Python313/python.exe", "bridge": "C:/agent-bridge/agent_bridge.py",
                "shell": "powershell"}}}

`shell` is the remote login shell: "posix" (default; macOS, Linux) or "powershell" (Windows OpenSSH default).
`routing` (optional) is printed by `dispatch.py machines`, so an agent learns the owner's placement rules first.
Python 3.9+, standard library only.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30']


def ssh_binary():
    if os.environ.get('AGENT_BRIDGE_SSH'):
        return os.environ['AGENT_BRIDGE_SSH']
    if os.name == 'nt':  # the Windows OpenSSH client, not a Git Bash/MSYS ssh that may be first on PATH
        system = Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'System32' / 'OpenSSH' / 'ssh.exe'
        if system.exists():
            return str(system)
    return 'ssh'


def machines_path():
    if os.environ.get('AGENT_BRIDGE_MACHINES'):
        return Path(os.environ['AGENT_BRIDGE_MACHINES'])
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', str(Path.home()))) / 'agent-bridge' / 'machines.json'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'agent-bridge' / 'machines.json'


def load_registry():
    path = machines_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        die('cannot read %s: %s' % (path, error))
    machines = data.get('machines') if isinstance(data, dict) else None
    if not isinstance(machines, dict) or not machines:
        die('%s has no "machines"' % path)
    return data


def die(message, code=2):
    sys.stdout.buffer.write((json.dumps({'ok': False, 'error': message}, ensure_ascii=False) + '\n').encode('utf-8'))
    sys.exit(code)


def posix_word(value):
    if value == '~' or value.startswith('~/'):  # keep the remote home expansion
        return '"$HOME"' + (shlex.quote(value[1:]) if len(value) > 1 else '')
    return shlex.quote(value)


def powershell_word(value):
    return "'" + value.replace("'", "''") + "'"


def split_prompt(args):
    """Remove --prompt/--prompt-file from args; return (args, prompt text or None)."""
    out, prompt, i = [], None, 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition('=')
        if name in ('--prompt', '--prompt-file'):
            if not eq:
                if i + 1 >= len(args):
                    die('%s needs a value' % name)
                value, i = args[i + 1], i + 1
            if name == '--prompt-file' and value != '-':
                prompt = Path(os.path.expanduser(value)).read_text(encoding='utf-8')
            elif value != '-':
                prompt = value
        else:
            out.append(arg)
        i += 1
    return out, prompt


def check_git_bash_rewrites(args):
    """Git Bash rewrites /posix/paths into C:/... and an unquoted ~ into /c/Users/...; remote paths would break."""
    if not os.environ.get('MSYSTEM'):
        return
    for arg in args:
        if re.match(r'^/[a-zA-Z]/(Users|home)/', arg):
            die('Git Bash expanded ~ in %r to the local home. Quote remote paths, for example --cwd \'~/Projects/app\'.' % arg)
        if re.match(r'^[A-Za-z]:[/\\].*[/\\](Git|msys64|usr)[/\\]', arg) or re.match(r'^[A-Za-z]:[/\\]Program Files[/\\]Git', arg):
            die('Git Bash rewrote %r into a Windows path. Prefix the command with MSYS_NO_PATHCONV=1 and quote remote '
                "paths, for example --cwd '~/Projects/app'." % arg)


def run_on(name, machine, args, prompt, timeout=None):
    python, bridge = machine.get('python') or 'python3', machine.get('bridge')
    if not bridge:
        die('machine %s has no "bridge" path' % name)
    stdin = prompt.encode('utf-8') if prompt is not None else None
    if prompt is not None:
        args = args + ['--prompt', '-']
    if machine.get('local'):
        argv = [os.path.expanduser(python), os.path.expanduser(bridge)] + args
    else:
        if machine.get('shell') == 'powershell':
            remote = '& ' + ' '.join(powershell_word(part) for part in [python, bridge] + args)
        else:
            remote = ' '.join(posix_word(part) for part in [python, bridge] + args)
        no_stdin = [] if stdin is not None else ['-n']  # Windows OpenSSH can hang on an idle stdin without -n
        argv = [ssh_binary()] + SSH_OPTIONS + no_stdin + machine.get('ssh_options', []) + [machine.get('ssh') or name, remote]
    # Windows OpenSSH stalls when its stdout is a pipe from Python and never forwards EOF from a stdin pipe:
    # give it plain files on both ends (on every platform, for one code path)
    feed_path = None
    if stdin is not None:
        with tempfile.NamedTemporaryFile(prefix='dispatch-', suffix='.txt', delete=False) as handle:
            handle.write(stdin)
            feed_path = handle.name
    try:
        with tempfile.TemporaryFile() as out, open(feed_path or os.devnull, 'rb') as feed:
            try:
                result = subprocess.run(argv, stdin=feed, stdout=out, stderr=subprocess.PIPE,
                                        timeout=timeout or machine.get('timeout', 900))
            except subprocess.TimeoutExpired:
                return 124, json.dumps({'ok': False, 'machine': name, 'error': 'timed out'}).encode('utf-8')
            except OSError as error:
                return 127, json.dumps({'ok': False, 'machine': name, 'error': str(error)}).encode('utf-8')
            out.seek(0)
            output = out.read()
    finally:
        if feed_path:
            os.unlink(feed_path)
    if not output.strip():
        message = result.stderr.decode('utf-8', 'replace').strip()[-600:] or 'no output (exit %d)' % result.returncode
        output = json.dumps({'ok': False, 'machine': name, 'error': message}, ensure_ascii=False).encode('utf-8')
    return result.returncode, output


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        sys.stdout.write(__doc__)
        return 0
    registry = load_registry()
    machines = registry['machines']
    if argv[0] == 'machines':
        rows = {name: {k: v for k, v in m.items() if k in ('ssh', 'local', 'shell', 'note')} for name, m in machines.items()}
        listing = {'ok': True, 'machines': rows}
        if registry.get('routing'):
            listing['routing'] = registry['routing']
        sys.stdout.buffer.write((json.dumps(listing, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))
        return 0
    if argv[0] == 'status':
        names = argv[1:] or list(machines)
        report = {}
        for name in names:
            if name not in machines:
                die('unknown machine: %s (known: %s)' % (name, ', '.join(machines)))
            _code, output = run_on(name, machines[name], ['status'], None, timeout=120)
            try:
                report[name] = json.loads(output.decode('utf-8', 'replace'))
            except ValueError:
                report[name] = {'ok': False, 'error': output.decode('utf-8', 'replace')[-600:]}
        sys.stdout.buffer.write((json.dumps(report, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))
        return 0
    name, args = argv[0], argv[1:]
    if name not in machines:
        die('unknown machine: %s (known: %s)' % (name, ', '.join(machines)))
    if not args:
        die('missing agent_bridge command')
    check_git_bash_rewrites(args)
    args, prompt = split_prompt(args)
    if prompt is None and args[0] in ('start', 'send', 'notify') and not sys.stdin.isatty():
        prompt = sys.stdin.buffer.read().decode('utf-8', 'replace')
        prompt = prompt if prompt.strip() else None
    code, output = run_on(name, machines[name], args, prompt)
    sys.stdout.buffer.write(output if output.endswith(b'\n') else output + b'\n')
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
