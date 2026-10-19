#!/usr/bin/env python3
"""agent-bridge: start and continue Claude Code / Codex sessions on this machine for a remote dispatcher.

A dispatcher (for example a Hermes agent on another machine) calls this script over SSH. Every request
becomes a job. The agent CLI runs inside the logged-in desktop session (macOS: a transient launchd job in
gui/<uid>, so the login keychain and `open` work; Windows: a one-shot scheduled task with the user's
interactive token), its output is captured in the job folder, and when it ends the session can be moved
into the desktop app and the result reported back through a configured return command.

Python 3.9+, standard library only.
"""
import argparse
import base64
import datetime
import glob
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

IS_MAC = sys.platform == 'darwin'
IS_WIN = os.name == 'nt'
HOME = Path.home()
SCRIPT = Path(__file__).resolve()
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: console tools must not pop up on the user's desktop
DEFAULT_TIMEOUT_H = 12
SESSION_RE = re.compile(r'[0-9A-Za-z][0-9A-Za-z_-]{7,79}')
PERMISSION_MODES = ('default', 'acceptEdits', 'auto', 'plan', 'dontAsk')  # bypassPermissions needs local opt-in
PASS_ENV = ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'AGENT_BRIDGE_NO_OPEN')  # carried into launchd jobs, which start clean
RUNNER = 'agent-bridge-runner'  # runner.c, installed next to this script on macOS
MESSAGES = {
    'en': {'done': 'finished', 'failed': 'failed', 'blocked': 'blocked', 'cancelled': 'cancelled', 'continue': 'Continue', 'job': 'Job',
           'session': 'Session', 'desktop_claude': 'moved to Claude Desktop', 'desktop_codex': 'opened in the Codex app',
           'question': 'question', 'warning': 'waiting',
           'stalled': 'No output for %d min. On a Mac this usually means a privacy prompt (Desktop or Documents access) is '
                      'waiting on that machine: allow it there, or add agent-bridge-runner under Full Disk Access.'},
    'tr': {'done': 'bitti', 'failed': 'başarısız', 'blocked': 'engellendi', 'cancelled': 'iptal edildi', 'continue': 'Devam', 'job': 'İş',
           'session': 'Oturum', 'desktop_claude': "Claude Desktop'a taşındı", 'desktop_codex': 'Codex uygulamasında açıldı',
           'question': 'soru', 'warning': 'bekliyor',
           'stalled': "%d dakikadır çıktı yok. Mac'te bu genellikle o makinede bir izin penceresinin (Masaüstü ya da "
                      "Belgeler erişimi) beklediği anlamına gelir: orada izin verin ya da agent-bridge-runner'ı Tam Disk "
                      "Erişimi'ne ekleyin."},
}


# ---------------------------------------------------------------- paths, config, small helpers

def config_path():
    if IS_WIN:
        return Path(os.environ.get('APPDATA', str(HOME))) / 'agent-bridge' / 'config.json'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(HOME / '.config'))) / 'agent-bridge' / 'config.json'


def state_dir():
    if os.environ.get('AGENT_BRIDGE_STATE'):
        return Path(os.environ['AGENT_BRIDGE_STATE'])
    if IS_WIN:
        return Path(os.environ.get('LOCALAPPDATA', str(HOME))) / 'agent-bridge'
    return Path(os.environ.get('XDG_STATE_HOME', str(HOME / '.local' / 'state'))) / 'agent-bridge'


def jobs_dir():
    path = state_dir() / 'jobs'
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WIN:  # prompts and results are private: the folders are the owner's only (Windows: the profile ACL)
        for folder in (state_dir(), path):
            if folder.stat().st_mode & 0o077:
                os.chmod(str(folder), 0o700)
    return path


def load_config():
    path = Path(os.environ['AGENT_BRIDGE_CONFIG']) if os.environ.get('AGENT_BRIDGE_CONFIG') else config_path()
    cfg = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}  # tolerate a BOM (PowerShell 5)
    cfg.setdefault('machine', platform.node().split('.')[0].lower())
    cfg.setdefault('language', 'en')
    return cfg


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def expand(value):
    return os.path.expanduser(os.path.expandvars(value))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    for _ in range(50):  # Windows refuses to replace a file while another process is reading it
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(str(tmp), str(path))


def read_status(jd):
    """status.json of a job; retries the short window in which Windows cannot open a file being replaced."""
    path = jd / 'status.json'
    for _ in range(20):
        if not path.exists():
            return {}
        status = read_json(path)
        if isinstance(status, dict):
            return status
        time.sleep(0.1)
    return {}


def emit(data):
    """JSON on stdout, always UTF-8 (a Windows pipe would otherwise use the ANSI code page)."""
    sys.stdout.buffer.write((json.dumps(data, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))
    sys.stdout.flush()


def fail(message, code=2):
    emit({'ok': False, 'error': message})
    sys.exit(code)


def cli(cfg, name):
    """argv prefix for the claude/codex CLI: config value (string or list), known locations, then PATH."""
    value = cfg.get(name)
    if isinstance(value, list) and value:
        return [expand(v) for v in value]
    candidates = [expand(value)] if isinstance(value, str) and value else []
    if IS_WIN:
        if name == 'claude':
            candidates.append(str(HOME / '.local' / 'bin' / 'claude.exe'))
        else:  # the native exe, never the npm .cmd/.ps1 shims: cmd.exe would re-parse the arguments
            npm = Path(os.environ.get('APPDATA', str(HOME))) / 'npm' / 'node_modules' / '@openai' / 'codex'
            app = Path(os.environ.get('LOCALAPPDATA', str(HOME))) / 'OpenAI' / 'Codex' / 'bin'
            candidates += glob.glob(str(npm / 'node_modules' / '@openai' / 'codex-win32-*' / 'vendor' / '*' / 'bin' / 'codex.exe'))
            candidates += sorted(glob.glob(str(app / '*' / 'codex.exe')), key=os.path.getmtime, reverse=True)
    elif name == 'claude':
        candidates += [str(HOME / '.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude']
    else:
        candidates += [str(HOME / '.local/bin/codex'), str(HOME / '.codex/packages/standalone/current/bin/codex'),
                       '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                       '/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                       '/Applications/Codex++.app/Contents/Resources/codex', '/opt/homebrew/bin/codex',
                       '/usr/local/bin/codex']
    found = shutil.which(name)
    if found and not (IS_WIN and found.lower().endswith(('.cmd', '.bat', '.ps1'))):
        candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and (IS_WIN or os.access(candidate, os.X_OK)):
            return [candidate]
    return None


def login_path(cfg):
    """PATH for agent processes: configured, else the login shell's PATH, else a sane default."""
    if cfg.get('path'):
        return expand(cfg['path'])
    if not IS_WIN:
        try:
            out = subprocess.run([os.environ.get('SHELL', '/bin/zsh'), '-lic', 'printf "__P__%s__P__" "$PATH"'],
                                 capture_output=True, text=True, timeout=15).stdout
            match = re.search(r'__P__(.*?)__P__', out)
            if match and '/usr/bin' in match.group(1):
                return match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
        return ':'.join([str(HOME / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin',
                         '/usr/sbin', '/sbin'])
    return os.environ.get('PATH', '')


# ---------------------------------------------------------------- job bookkeeping

def new_job_id():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)


def job_dir(job_id):
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{4}', job_id or ''):
        fail('invalid job id: %r' % job_id)
    return jobs_dir() / job_id


def update_status(jd, **fields):
    status = read_status(jd)
    status.update(fields)
    write_json(jd / 'status.json', status)
    return status


def project_label(cwd):
    return Path(cwd).name or cwd


def read_prompt(args):
    if getattr(args, 'prompt_file', None) not in (None, '-'):
        text = Path(expand(args.prompt_file)).read_text(encoding='utf-8')
    elif getattr(args, 'prompt', None) not in (None, '-'):
        text = args.prompt
    else:
        text = sys.stdin.buffer.read().decode('utf-8', 'replace')
    text = text.strip()
    if not text:
        fail('empty prompt')
    return text


# ---------------------------------------------------------------- launching the worker

def launcher_kind():
    """'launchd' (macOS), 'task' (Windows) or 'direct'; AGENT_BRIDGE_LAUNCHER overrides the platform default."""
    choice = os.environ.get('AGENT_BRIDGE_LAUNCHER', '')
    if choice in ('direct', 'launchd', 'task'):
        return choice
    return 'launchd' if IS_MAC else 'task' if IS_WIN else 'direct'


def windows_session_id():
    import ctypes
    session = ctypes.c_ulong()
    if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return session.value
    return None


TASK_XML = '''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>agent-bridge job {job}; removed when the job ends</Description></RegistrationInfo>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden></Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{workdir}</WorkingDirectory></Exec></Actions>
</Task>
'''


def delete_windows_task(name, delay_s=0):
    if not delay_s:
        subprocess.run(['schtasks', '/Delete', '/TN', name, '/F'], capture_output=True, creationflags=NO_WINDOW)
        return
    # the worker is the task's own process: delete the definition shortly after it exits
    subprocess.Popen(['cmd', '/c', 'ping -n %d 127.0.0.1 >nul & schtasks /Delete /TN %s /F >nul 2>&1' % (delay_s + 1, name)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=NO_WINDOW | 0x00000008 | 0x00000200, close_fds=True)


def launch_windows_task(jd, job_id, argv):
    """Run the worker in the logged-in user's desktop session (works from an SSH session too)."""
    whoami = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True,
                            errors='replace', creationflags=NO_WINDOW).stdout
    sid = re.search(r'"(S-1-[0-9-]+)"', whoami)
    if not sid:
        return None
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    name = 'agent-bridge\\' + job_id
    (jd / 'task.xml').write_text(TASK_XML.format(
        job=job_id, user=sid.group(1), command=xml_escape(str(pythonw if pythonw.exists() else sys.executable)),
        arguments=xml_escape(subprocess.list2cmdline(argv[1:])), workdir=xml_escape(str(jd))), encoding='utf-16')
    update_status(jd, launcher='task', task_name=name)
    for step in (['/Create', '/TN', name, '/XML', str(jd / 'task.xml'), '/F'], ['/Run', '/TN', name]):
        result = subprocess.run(['schtasks'] + step, capture_output=True, text=True, errors='replace', creationflags=NO_WINDOW)
        if result.returncode != 0:
            delete_windows_task(name)
            update_status(jd, task_name=None, launcher_warning='schtasks %s: %s' % (step[0], (result.stderr or result.stdout).strip()[:200]))
            return None
    deadline = time.time() + 20
    while time.time() < deadline:
        if (jd / 'worker.lock').exists():
            return 'task (desktop session)'
        time.sleep(0.5)
    delete_windows_task(name)  # an interactive-token task only starts while the user is logged on
    update_status(jd, task_name=None, launcher_warning='scheduled task did not start; nobody logged on to the desktop?')
    return None


def launchd_env(path, jd=None):
    env = {'PATH': path, 'HOME': str(HOME), 'LANG': os.environ.get('LANG') or 'en_US.UTF-8'}
    env.update({key: os.environ[key] for key in PASS_ENV if os.environ.get(key)})
    if jd is not None:
        env['AGENT_BRIDGE_JOB'] = jd.name
    return env


def launch(jd, job_id, cfg):
    """Start `_run JOB` detached, inside the desktop session when possible. Returns a short description."""
    config_file = str(Path(os.environ.get('AGENT_BRIDGE_CONFIG') or config_path()))
    argv = [sys.executable, str(SCRIPT), '_run', job_id, '--state', str(state_dir()), '--config', config_file]
    env_path = login_path(cfg)
    kind = launcher_kind()
    if kind == 'launchd' and IS_MAC:
        label = 'agent-bridge.' + job_id
        plist = {'Label': label, 'ProgramArguments': argv, 'RunAtLoad': True, 'AbandonProcessGroup': True,
                 'WorkingDirectory': str(jd), 'StandardOutPath': str(jd / 'worker.log'),
                 'StandardErrorPath': str(jd / 'worker.log'),
                 'EnvironmentVariables': launchd_env(env_path)}
        (jd / 'job.plist').write_bytes(plistlib.dumps(plist))
        update_status(jd, launcher='launchd-gui', launchd_label=label)  # before the worker starts writing status
        result = subprocess.run(['launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(jd / 'job.plist')],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return 'launchd gui/%d' % os.getuid()
        update_status(jd, launchd_label=None, launcher_warning='launchctl bootstrap failed: ' + (result.stderr or '').strip()[:200])
    elif kind == 'task' and IS_WIN:
        how = launch_windows_task(jd, job_id, argv)
        if how:
            return how
    env = dict(os.environ, PATH=env_path)
    log = open(str(jd / 'worker.log'), 'ab')
    update_status(jd, launcher='detached')
    if IS_WIN:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(argv, cwd=str(jd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=flags | 0x01000000, close_fds=True)  # + CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(argv, cwd=str(jd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(argv, cwd=str(jd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    return 'detached'


def create_job(cfg, request, prompt):
    job_id = new_job_id()
    jd = jobs_dir() / job_id
    jd.mkdir(parents=True)
    (jd / 'prompt.txt').write_text(prompt, encoding='utf-8')
    request.update(job=job_id, machine=cfg['machine'], created=now(), requester=os.environ.get('AGENT_BRIDGE_REQUESTER', ''))
    write_json(jd / 'request.json', request)
    write_json(jd / 'status.json', {'job': job_id, 'machine': cfg['machine'], 'harness': request['harness'],
                                    'action': request['action'], 'cwd': request.get('cwd'), 'state': 'queued',
                                    'created': request['created'], 'session': request.get('session')})
    how = launch(jd, job_id, cfg)
    emit({'ok': True, 'job': job_id, 'machine': cfg['machine'], 'harness': request['harness'],
          'action': request['action'], 'launcher': how})


# ---------------------------------------------------------------- macOS: agents as their own launchd jobs
#
# macOS privacy controls (Desktop/Documents folder access, Full Disk Access) judge the "responsible" process. A child
# of this worker would be judged as the worker's Python; started by launchd, the agent CLI is responsible for itself,
# so the grants and prompts name `claude` or `codex`, as when the user runs them. The gui/<uid> domain also gives
# the login keychain (Claude sign-in, code signing).
#
# A grant for a command-line tool belongs to its path, and Claude Code moves to a new versioned path with every
# update. When agent-bridge-runner (runner.c) is installed and holds Full Disk Access, launchd starts the runner and
# the agent runs as its child, judged by the runner's grant, which survives updates.

def runner_path(cfg):
    if not IS_MAC or cfg.get('mac_runner') is False:
        return None
    path = expand(cfg['mac_runner']) if isinstance(cfg.get('mac_runner'), str) else str(SCRIPT.with_name(RUNNER))
    return path if os.access(path, os.X_OK) else None


def runner_access(path, folder):
    """Whether the runner, started by launchd as its own responsible process, holds Full Disk Access (None: unknown)."""
    label = 'agent-bridge.%s.check' % folder.name
    if not launchd_spawn(folder, label, [path, '--check'], str(HOME), '/dev/null', '/dev/null', '/dev/null'):
        return None
    try:
        code = launchd_wait(label, time.time() + 20, lambda: None)
    finally:
        launchd_remove(label)
    return {0: True, 1: False}.get(code)


def job_runner(cfg, jd):
    """argv prefix for this job's agent processes: the runner when it may be used; decided once per job."""
    status = read_status(jd)
    if 'runner' not in status:
        path, mode = runner_path(cfg), cfg.get('mac_runner_mode', 'auto')
        access = runner_access(path, jd) if path and mode != 'always' else None
        status = update_status(jd, runner=path if path and (mode == 'always' or access) else None, runner_access=access)
    return [status['runner']] if status.get('runner') else []


def launchd_print(label):
    out = subprocess.run(['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True, text=True,
                         errors='replace').stdout

    def field(key):
        match = re.search(r'^\t%s = (.+)$' % re.escape(key), out, re.M)
        return match.group(1).strip() if match else None
    return field('state'), field('pid'), field('last exit code'), field('last terminating signal')


def launchd_spawn(jd, label, argv, cwd, stdin, stdout, stderr):
    env = launchd_env(os.environ.get('PATH', ''), jd)
    plist = {'Label': label, 'ProgramArguments': argv, 'WorkingDirectory': cwd, 'RunAtLoad': True,
             'ProcessType': 'Interactive', 'StandardInPath': stdin, 'StandardOutPath': stdout,
             'StandardErrorPath': stderr, 'EnvironmentVariables': env}
    path = jd / (label.rsplit('.', 1)[-1] + '.plist')
    path.write_bytes(plistlib.dumps(plist))
    result = subprocess.run(['launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        update_status(jd, launcher_warning='launchctl bootstrap %s: %s' % (label, (result.stderr or '').strip()[:200]))
    return result.returncode == 0


def launchd_remove(label):
    subprocess.run(['launchctl', 'bootout', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True)


def launchd_wait(label, deadline, poll):
    """Poll until the job exits: its exit code, minus the signal number when killed, -9 after the deadline."""
    while True:
        poll()
        state, _pid, last_exit, signal_name = launchd_print(label)
        if state is None:
            return -1
        if state == 'not running' and (signal_name or (last_exit and last_exit != '(never exited)')):
            poll()
            if signal_name:
                number = re.search(r'(\d+)\s*$', signal_name)
                return -int(number.group(1)) if number else -1
            return int(last_exit) if re.fullmatch(r'-?\d+', last_exit) else -1
        if time.time() > deadline:
            launchd_remove(label)
            poll()
            return -9
        time.sleep(1)


def run_stream_launchd(cfg, cmd, cwd, prompt, jd, on_event, timeout_s, on_stall):
    label = 'agent-bridge.%s.agent' % jd.name
    out_path = jd / 'output.jsonl'
    out_path.touch()
    stdin = str(jd / 'prompt.txt') if prompt is not None else '/dev/null'
    if not launchd_spawn(jd, label, job_runner(cfg, jd) + cmd, cwd, stdin, str(out_path), str(jd / 'stderr.log')):
        return None
    update_status(jd, agent_label=label)
    if cancelled(jd):  # a cancel that came while the agent was being started
        launchd_remove(label)
    tail = {'offset': 0, 'rest': b'', 'since': time.time(), 'stall_s': float(cfg.get('stall_warning_s', 180) or 0)}

    def poll(final=False):
        with open(str(out_path), 'rb') as handle:
            handle.seek(tail['offset'])
            chunk = handle.read()
        tail['offset'] += len(chunk)
        lines = (tail['rest'] + chunk).split(b'\n')
        tail['rest'] = b'' if final else lines.pop()
        for raw in lines:
            feed_event(raw.decode('utf-8', 'replace'), on_event)
        # the CLIs print their first event at once; silence from the start means the agent is blocked, usually on a
        # privacy prompt nobody sees from a phone
        if tail['stall_s'] and not tail['offset'] and time.time() - tail['since'] > tail['stall_s']:
            tail['stall_s'] = 0
            on_stall(time.time() - tail['since'])
    try:
        return launchd_wait(label, time.time() + timeout_s, poll)
    finally:
        launchd_remove(label)
        update_status(jd, agent_label=None)
        poll(final=True)


def run_tty_launchd(cfg, jd, label, argv, cwd, timeout_s=120):
    """Run argv as its own launchd job on a pseudo-terminal (for commands that insist on a terminal)."""
    import fcntl
    import select
    import struct
    import termios
    master, slave = os.openpty()
    chunks = []

    def poll():
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        tty = os.ttyname(slave)
        if not launchd_spawn(jd, label, job_runner(cfg, jd) + argv, cwd, tty, tty, tty):
            return None, ''
        code = launchd_wait(label, time.time() + timeout_s, poll)
        return code, re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\r', '', b''.join(chunks).decode('utf-8', 'replace'))
    finally:
        launchd_remove(label)
        os.close(master)
        os.close(slave)


# ---------------------------------------------------------------- the worker

def feed_event(line, on_event):
    try:
        event = json.loads(line)
    except ValueError:
        return
    if isinstance(event, dict):
        on_event(event)


def warn_stalled(cfg, req, jd, seconds):
    words = MESSAGES.get(cfg.get('language'), MESSAGES['en'])
    text = words['stalled'] % max(1, round(seconds / 60))
    status = update_status(jd, warning=text)
    if req.get('notify'):
        update_status(jd, warning_notify=notify(cfg, status, compose_message(cfg, status, kind='warning', text=text),
                                                kind='warning'))


def run_stream(cfg, req, cmd, prompt, jd, on_event):
    cwd, timeout_s = req['cwd'], req.get('timeout_h', DEFAULT_TIMEOUT_H) * 3600
    if cancelled(jd):
        return -15
    if IS_MAC and launcher_kind() == 'launchd':
        code = run_stream_launchd(cfg, cmd, cwd, prompt, jd, on_event, timeout_s,
                                  lambda seconds: warn_stalled(cfg, req, jd, seconds))
        if code is not None:
            return code
    env = dict(os.environ, AGENT_BRIDGE_JOB=jd.name)
    with open(str(jd / 'output.jsonl'), 'a', encoding='utf-8') as out, open(str(jd / 'stderr.log'), 'a', encoding='utf-8') as err:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        update_status(jd, agent_pid=proc.pid)
        if cancelled(jd):
            proc.kill()
        watchdog = threading.Timer(timeout_s, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            if prompt is not None:
                try:
                    proc.stdin.write(prompt)
                except OSError:  # the agent is already gone (killed or failed at start)
                    pass
            proc.stdin.close()
            for line in proc.stdout:
                out.write(line)
                out.flush()
                feed_event(line, on_event)
            return proc.wait()
        finally:
            watchdog.cancel()
            proc.wait()
            update_status(jd, agent_pid=None)  # a cancel must never signal a recycled pid


def allowed_permission_modes(cfg):
    return PERMISSION_MODES + (('bypassPermissions',) if cfg.get('allow_bypass_permissions') is True else ())


def run_claude(cfg, req, jd, prompt):
    base = cli(cfg, 'claude')
    if not base:
        return {'state': 'failed', 'error': 'claude CLI not found'}
    mode = req.get('permission_mode') or cfg.get('claude_permission_mode', 'auto')
    if mode not in allowed_permission_modes(cfg):
        return {'state': 'failed', 'error': 'permission mode not allowed on this machine: %s' % mode}
    cmd = base + ['-p', '--output-format', 'stream-json', '--verbose', '--permission-mode', mode]
    if req.get('model'):
        cmd += ['--model', req['model']]
    if req.get('session'):
        cmd += ['--resume', req['session']]
    if req.get('name') and not req.get('session'):
        cmd += ['--name', req['name']]
    found = {'result': None, 'text': ''}

    def on_event(event):
        if event.get('session_id') and not read_status(jd).get('session'):
            update_status(jd, session=event['session_id'])
        if event.get('type') == 'assistant':
            for block in (event.get('message') or {}).get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'text' and block.get('text', '').strip():
                    found['text'] = block['text']
        if event.get('type') == 'result':
            found['result'] = event

    code = run_stream(cfg, req, cmd, prompt, jd, on_event)
    result = found['result'] or {}
    text = result.get('result') or found['text']
    ok = code == 0 and result and not result.get('is_error')
    return {'state': 'done' if ok else 'failed', 'exit_code': code, 'result': text,
            'session': result.get('session_id') or read_status(jd).get('session'),
            'cost_usd': result.get('total_cost_usd')}


def run_codex_exec(cfg, req, jd, prompt):
    base = cli(cfg, 'codex')
    if not base:
        return {'state': 'failed', 'error': 'codex CLI not found'}
    model = req.get('model') or cfg.get('codex_model')
    opts = ['--json', '--skip-git-repo-check', '-o', str(jd / 'last-message.txt')]
    if model:
        opts += ['-m', model]
    if req.get('session'):
        cmd = base + ['exec', 'resume'] + opts + [req['session'], '-']
    else:
        cmd = base + ['exec'] + opts + ['-C', req['cwd'], '-']
    found = {'text': '', 'failed': None, 'completed': False}

    def on_event(event):
        if event.get('type') == 'thread.started' and event.get('thread_id'):
            update_status(jd, session=event['thread_id'])
        item = event.get('item') or {}
        if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            found['text'] = item.get('text', '')
        if event.get('type') == 'turn.completed':
            found['completed'] = True
        if event.get('type') == 'turn.failed':
            found['failed'] = (event.get('error') or {}).get('message', 'turn failed')

    code = run_stream(cfg, req, cmd, prompt, jd, on_event)
    last = (jd / 'last-message.txt')
    text = last.read_text(encoding='utf-8').strip() if last.exists() else found['text']
    ok = code == 0 and found['completed'] and not found['failed']
    return {'state': 'done' if ok else 'failed', 'exit_code': code, 'result': text or found['failed'] or '',
            'session': read_status(jd).get('session')}


def codex_rollout(thread_id):
    root = Path(expand(os.environ.get('CODEX_HOME', str(HOME / '.codex')))) / 'sessions'
    hits = glob.glob(str(root / '**' / ('rollout-*%s.jsonl' % thread_id)), recursive=True)
    return Path(max(hits, key=os.path.getmtime)) if hits else None


def open_url(url):
    if os.environ.get('AGENT_BRIDGE_NO_OPEN'):
        return True
    if IS_MAC:
        return subprocess.run(['open', '-g', url], capture_output=True, text=True).returncode == 0
    if IS_WIN:
        os.startfile(url)  # noqa: S606 - launching the registered desktop handler is the point
        return True
    return subprocess.run(['xdg-open', url], capture_output=True).returncode == 0


def run_codex_queue(cfg, req, jd, prompt):
    """Queue a message into an existing Codex desktop thread, wake the app, wait for that turn to finish."""
    base = cli(cfg, 'codex')
    thread = req['session']
    rollout = codex_rollout(thread)
    if not base or not rollout:
        return {'state': 'failed', 'error': 'codex CLI or rollout for thread %s not found' % thread}
    offset = rollout.stat().st_size
    # `codex queue` takes the message only as an argument; the `=` form keeps a leading '-' from reading as an option.
    # The text is on that short-lived process's command line (see README, Security).
    queued = subprocess.run(base + ['queue', '--thread=' + thread, '--message=' + prompt], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    if queued.returncode != 0:
        return {'state': 'failed', 'error': 'codex queue: ' + (queued.stderr or queued.stdout).strip()[:300]}
    update_status(jd, session=thread, queued=(queued.stdout or '').strip()[:200])
    open_url('codex://threads/' + thread)
    deadline = time.time() + req.get('timeout_h', DEFAULT_TIMEOUT_H) * 3600
    started = False
    while time.time() < deadline and not cancelled(jd):  # a cancel stops the wait; the turn stays in the app
        time.sleep(5)
        with open(str(rollout), 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read()
        offset += len(chunk)
        for raw in chunk.decode('utf-8', 'replace').splitlines():
            try:
                payload = (json.loads(raw).get('payload') or {})
            except ValueError:
                continue
            if payload.get('type') == 'task_started':
                started = True
                update_status(jd, desktop_turn='running')
            if payload.get('type') == 'task_complete' and started:
                return {'state': 'done', 'exit_code': 0, 'result': payload.get('last_agent_message') or '', 'session': thread}
            if payload.get('type') == 'turn_aborted' and started:
                return {'state': 'failed', 'exit_code': 1, 'result': 'turn aborted', 'session': thread}
    return {'state': 'failed', 'exit_code': -9, 'result': 'timed out waiting for the Codex app', 'session': thread}


def move_to_desktop(cfg, harness, session, jd, cwd):
    """Hand a Claude session to Claude Desktop, or open a Codex thread in the Codex app."""
    if not session:
        return 'skipped: no session'
    if not SESSION_RE.fullmatch(session):
        return 'failed: invalid session id'
    if harness == 'codex':
        return 'opened' if open_url('codex://threads/' + session) else 'failed: open'
    base = cli(cfg, 'claude')
    if not base:
        return 'failed: claude CLI not found'
    argv = base + ['--desktop', '--resume', session]  # run in the session's folder, where Claude looks it up
    cwd = cwd or str(HOME)  # not checked here: on macOS that check alone would ask for privacy access as Python
    if IS_MAC and launcher_kind() == 'launchd':  # --desktop insists on a terminal: give it a pty
        code, text = run_tty_launchd(cfg, jd, 'agent-bridge.%s.desktop' % jd.name, argv, cwd)
        if code is not None:
            return 'moved' if code == 0 else 'failed: ' + text.strip()[-200:]
    if IS_MAC:
        cmd = ['/usr/bin/script', '-q', '/dev/null'] + argv
    elif IS_WIN:  # a new console window is its terminal; the empty argument is start's window title
        cmd = ['cmd', '/c', 'start', '', '/min'] + argv
    else:
        return 'skipped: unsupported platform'
    result = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                            timeout=120, creationflags=NO_WINDOW)
    return 'moved' if result.returncode == 0 else 'failed: ' + (result.stdout + result.stderr).strip()[-200:]


def compose_message(cfg, status, kind='result', text=None):
    words = MESSAGES.get(cfg.get('language'), MESSAGES['en'])
    harness = 'Claude' if status.get('harness') == 'claude' else 'Codex'
    head = '[%s · %s] %s: %s' % (cfg['machine'], harness, project_label(status.get('cwd') or ''),
                                 words[kind] if kind in ('question', 'warning') else words.get(status.get('state'), status.get('state')))
    body = (text if text is not None else status.get('result') or status.get('error') or '').strip()
    limit = int(cfg.get('notify_max_chars', 1500))
    if len(body) > limit:
        body = body[:limit].rstrip() + ' …'
    lines = [head, body] if body else [head]
    meta = '%s: %s' % (words['job'], status.get('job'))
    if status.get('session'):
        meta += ' · %s: %s' % (words['session'], status['session'])
    lines.append(meta)
    if str(status.get('desktop', '')).startswith(('moved', 'opened')):
        lines.append(words['desktop_claude'] if status.get('harness') == 'claude' else words['desktop_codex'])
    if status.get('harness') == 'codex' and status.get('session'):
        lines.append('%s: codex://threads/%s' % (words['continue'], status['session']))
    return '\n'.join(lines)


def notify(cfg, status, message, kind='result'):
    command = cfg.get('notify_command')
    if not command:
        return 'skipped: no notify_command'
    payload = {'message': message, 'kind': kind, 'job': status.get('job'), 'machine': cfg['machine'],
               'harness': status.get('harness'), 'session': status.get('session'), 'state': status.get('state'),
               'cwd': status.get('cwd')}
    argv = [expand(part) for part in command] if isinstance(command, list) else shlex.split(command)
    try:
        result = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return 'failed: %s' % error
    if result.returncode == 0:
        return 'sent'
    return 'failed: ' + (result.stderr or result.stdout).decode('utf-8', 'replace').strip()[-300:]


def cleanup_launcher(jd, status):
    label = status.get('launchd_label')
    if IS_MAC and label:  # bootout after this process has exited; AbandonProcessGroup keeps the helper alive
        subprocess.Popen(['/bin/sh', '-c', 'sleep 3; launchctl bootout gui/%d/%s >/dev/null 2>&1' % (os.getuid(), label)],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    if IS_WIN and status.get('task_name'):
        delete_windows_task(status['task_name'], delay_s=3)


def claim(jd):
    """Exactly one worker per job, even if a late scheduled task and a fallback launch both start."""
    try:
        os.close(os.open(str(jd / 'worker.lock'), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def cancelled(jd):
    return (jd / 'cancel').exists()  # a marker file: status.json is rewritten by the worker and would lose a field


def cancel_job(jd):
    """Stop a job: no agent starts after this, a running agent is ended, the worker then reports `cancelled`."""
    (jd / 'cancel').write_text(now(), encoding='utf-8')
    status = read_status(jd)
    if IS_MAC and status.get('agent_label'):
        launchd_remove(status['agent_label'])
    elif status.get('agent_pid'):
        if IS_WIN:
            subprocess.run(['taskkill', '/PID', str(status['agent_pid']), '/T', '/F'], capture_output=True,
                           creationflags=NO_WINDOW)
        else:
            try:
                os.kill(int(status['agent_pid']), 15)
            except OSError:
                pass


def run_job(job_id):
    cfg = load_config()
    jd = job_dir(job_id)
    req = read_json(jd / 'request.json')
    prompt = (jd / 'prompt.txt').read_text(encoding='utf-8') if (jd / 'prompt.txt').exists() else None
    if not claim(jd):
        return
    status = update_status(jd, state='running', started=now(), worker_pid=os.getpid())
    try:
        if cancelled(jd):
            outcome = {'state': 'cancelled'}
        elif req['action'] == 'desktop':
            outcome = {'state': 'done', 'session': req['session'],
                       'desktop': move_to_desktop(cfg, req['harness'], req['session'], jd, req.get('cwd'))}
            outcome['result'] = outcome['desktop']
        elif req['harness'] == 'claude':
            outcome = run_claude(cfg, req, jd, prompt)
        elif req['action'] == 'send':
            outcome = run_codex_queue(cfg, req, jd, prompt)
        else:
            outcome = run_codex_exec(cfg, req, jd, prompt)
    except Exception as error:  # report every worker failure in the job status
        outcome = {'state': 'failed', 'error': '%s: %s' % (type(error).__name__, error)}
    if cancelled(jd):
        outcome['state'] = 'cancelled'
    result_text = outcome.pop('result', '') or ''
    (jd / 'result.md').write_text(result_text, encoding='utf-8')
    status = update_status(jd, ended=now(), result_preview=result_text[:500], **outcome)
    if req.get('desktop') and req['action'] != 'desktop' and status.get('state') == 'done':
        status = update_status(jd, desktop=move_to_desktop(cfg, req['harness'], status.get('session'), jd, req.get('cwd')))
    if req.get('notify'):
        status['result'] = result_text
        status = update_status(jd, notify=notify(cfg, status, compose_message(cfg, status)))
    status = update_status(jd, finished=now())  # `job wait` returns after the desktop move and the report too
    cleanup_launcher(jd, status)


# ---------------------------------------------------------------- commands

def folder_exists(path):
    if os.path.isdir(path):
        return True
    try:
        os.listdir(path)
    except PermissionError:  # macOS privacy controls hide it from this process (for example an SSH login without
        return True          # Full Disk Access); the agent's own launchd job may still be allowed in
    except OSError:
        return False
    return True


def cmd_start(args, cfg, action):
    cwd = os.path.realpath(expand(args.cwd)) if getattr(args, 'cwd', None) else None
    session = getattr(args, 'session', None)
    if session is not None and not SESSION_RE.fullmatch(session):
        fail('invalid session id')
    mode = getattr(args, 'permission_mode', None)
    if mode and mode not in allowed_permission_modes(cfg):
        fail('permission mode not allowed on this machine: %s (bypassPermissions needs "allow_bypass_permissions": true '
             'in the local config)' % mode)
    if action == 'start':
        if not cwd or not folder_exists(cwd):
            fail('cwd does not exist: %s' % cwd)
    else:
        if not session:
            fail('--session is required')
        if not cwd:
            cwd = session_cwd(args.harness, session) or str(HOME)
    if action == 'send' and args.harness == 'claude' and not args.force and claude_session_busy(session):
        fail('this Claude session is open in a running process (desktop or terminal); start a new session that '
             'references it, or retry with --force', 3)
    prompt = read_prompt(args) if action != 'desktop' else None
    request = {'harness': args.harness, 'action': action, 'cwd': cwd, 'session': session,
               'model': getattr(args, 'model', None), 'permission_mode': getattr(args, 'permission_mode', None),
               'name': getattr(args, 'name', None), 'desktop': bool(getattr(args, 'desktop', False)),
               'notify': bool(getattr(args, 'notify', False)), 'timeout_h': getattr(args, 'timeout_h', None) or DEFAULT_TIMEOUT_H}
    create_job(cfg, request, prompt or '')


def session_cwd(harness, session):
    for jd in sorted(jobs_dir().iterdir(), reverse=True):  # jobs this bridge ran know their folder
        status = read_json(jd / 'status.json', {}) or {}
        if status.get('session') == session and status.get('harness') == harness and status.get('cwd'):
            return status['cwd']
    if harness == 'codex':
        rollout = codex_rollout(session)
        if rollout:
            with open(str(rollout), encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'session_meta':
                        return (event.get('payload') or {}).get('cwd')
        return None
    for path in glob.glob(str(HOME / '.claude' / 'projects' / '*' / ('%s.jsonl' % session))):
        with open(path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    cwd = json.loads(line).get('cwd')
                except ValueError:
                    continue
                if cwd:
                    return cwd
    return None


def process_command_lines():
    if not IS_WIN:
        return subprocess.run(['ps', '-axo', 'command'], capture_output=True, text=True, errors='replace').stdout
    script = 'Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }'
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    for cmd in (['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                ['wmic', 'process', 'get', 'CommandLine']):  # wmic is missing on newer Windows
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=60, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return result.stdout
    return ''


def claude_session_busy(session):
    return any(session in line and 'claude' in line.lower() and 'agent_bridge' not in line
               for line in process_command_lines().splitlines())


def finished(status):
    if status.get('finished'):
        return True
    if status.get('state', 'queued') in ('queued', 'running'):
        return False
    try:  # ended, but the worker died before marking itself finished: give up waiting after a while
        ended = datetime.datetime.fromisoformat(status.get('ended') or '')
    except ValueError:
        return True
    return (datetime.datetime.now().astimezone() - ended).total_seconds() > 180


def cmd_job(args, cfg):
    if args.job_action == 'list':
        rows = []
        for jd in sorted(jobs_dir().iterdir(), reverse=True)[:args.limit]:
            status = read_json(jd / 'status.json', {}) or {}
            rows.append({k: status.get(k) for k in ('job', 'state', 'harness', 'action', 'cwd', 'session', 'created', 'ended')})
        emit({'ok': True, 'jobs': rows})
        return
    jd = job_dir(args.job)
    if not jd.exists():
        fail('unknown job: %s' % args.job)
    if args.job_action == 'cancel':
        if finished(read_status(jd)):
            fail('job already finished: %s' % args.job)
        cancel_job(jd)
        deadline = time.time() + (60 if args.timeout is None else args.timeout)
        while time.time() < deadline and not finished(read_status(jd)):
            time.sleep(1)
    if args.job_action == 'wait':
        deadline = time.time() + (600 if args.timeout is None else args.timeout)
        while time.time() < deadline and not finished(read_status(jd)):
            time.sleep(3)
    status = read_status(jd)
    if args.job_action == 'result':
        status['result'] = (jd / 'result.md').read_text(encoding='utf-8') if (jd / 'result.md').exists() else None
    emit(dict(status, ok=True))


def cmd_notify(args, cfg):
    job = os.environ.get('AGENT_BRIDGE_JOB') or args.job
    status = {'job': job or '-', 'harness': args.harness, 'cwd': os.getcwd(), 'state': 'running'}
    if job and (jobs_dir() / job / 'status.json').exists():
        status.update(read_json(jobs_dir() / job / 'status.json', {}) or {})
    text = read_prompt(args)
    emit({'ok': True, 'notify': notify(cfg, status, compose_message(cfg, status, kind='question', text=text), kind='question')})


def version_of(argv):
    try:
        return subprocess.run(argv + ['--version'], capture_output=True, text=True, errors='replace', timeout=20,
                              creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def cmd_status(args, cfg):
    claude, codex = cli(cfg, 'claude'), cli(cfg, 'codex')
    info = {'ok': True, 'machine': cfg['machine'], 'os': platform.platform(), 'python': sys.version.split()[0],
            'claude': {'path': claude and ' '.join(claude), 'version': claude and version_of(claude)},
            'codex': {'path': codex and ' '.join(codex), 'version': codex and version_of(codex),
                      'queue': bool(codex) and '--thread' in subprocess.run(codex + ['queue', '--help'], capture_output=True,
                                                                             text=True, errors='replace', timeout=20,
                                                                             creationflags=NO_WINDOW).stdout},
            'notify_command': bool(cfg.get('notify_command')), 'state_dir': str(state_dir()), 'launcher': launcher_kind()}
    if IS_WIN:
        info['windows_session'] = windows_session_id()
    if IS_MAC:
        info['gui_session'] = subprocess.run(['launchctl', 'print', 'gui/%d' % os.getuid()], capture_output=True).returncode == 0
        info['apps'] = [name for name in ('Claude.app', 'Codex++.app', 'ChatGPT.app', 'Codex.app', 'Xcode.app', 'Android Studio.app')
                        if (Path('/Applications') / name).exists()]
        runner, access = runner_path(cfg), None
        if runner and info['gui_session'] and launcher_kind() == 'launchd':
            folder = state_dir() / 'checks' / ('%d-%s' % (os.getpid(), secrets.token_hex(2)))
            folder.mkdir(parents=True)
            try:
                access = runner_access(runner, folder)
            finally:
                shutil.rmtree(str(folder), ignore_errors=True)
        info['runner'] = {'path': runner, 'mode': cfg.get('mac_runner_mode', 'auto'), 'full_disk_access': access}
    running = [read_json(jd / 'status.json', {}) for jd in jobs_dir().iterdir()]
    info['jobs_running'] = sum(1 for s in running if s and s.get('state') in ('queued', 'running'))
    emit(info)


def decode_args(token):
    try:
        args = json.loads(base64.urlsafe_b64decode(token.encode('ascii')).decode('utf-8'))
    except (ValueError, UnicodeError):
        args = None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        fail('invalid --argv-b64 value')
    return args


def main(argv=None):
    parser = argparse.ArgumentParser(prog='agent-bridge', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='capabilities of this machine')
    for action in ('start', 'send'):
        p = sub.add_parser(action, help='start a new session' if action == 'start' else 'send a follow-up to a session')
        p.add_argument('harness', choices=('claude', 'codex'))
        if action == 'start':
            p.add_argument('--cwd', required=True)
            p.add_argument('--name')
        else:
            p.add_argument('--session', required=True, help='Claude session id or Codex thread id')
            p.add_argument('--cwd')
            p.add_argument('--force', action='store_true', help='send even if the Claude session is open elsewhere')
        p.add_argument('--prompt', help="prompt text; omit or '-' to read stdin")
        p.add_argument('--prompt-file')
        p.add_argument('--model')
        p.add_argument('--permission-mode')
        p.add_argument('--desktop', action='store_true', help='afterwards move/open the session in the desktop app')
        p.add_argument('--notify', action='store_true', help='report the result through notify_command')
        p.add_argument('--timeout-h', type=float)
    p = sub.add_parser('desktop', help='move a Claude session into Claude Desktop / open a Codex thread in its app')
    p.add_argument('harness', choices=('claude', 'codex'))
    p.add_argument('--session', required=True)
    p = sub.add_parser('job', help='inspect jobs')
    p.add_argument('job_action', choices=('show', 'result', 'wait', 'cancel', 'list'))
    p.add_argument('job', nargs='?')
    p.add_argument('--timeout', type=int, help='seconds to wait (wait: 600, cancel: 60 for the worker to report)')
    p.add_argument('--limit', type=int, default=20)
    p = sub.add_parser('notify', help='send a message (for example a question) back to the dispatcher')
    p.add_argument('--harness', default='claude', choices=('claude', 'codex'))
    p.add_argument('--job')
    p.add_argument('--prompt', help="message text; omit or '-' to read stdin")
    p.add_argument('--prompt-file')
    p = sub.add_parser('_run')
    p.add_argument('job')
    p.add_argument('--state')
    p.add_argument('--config')
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['--argv-b64']:  # dispatch.py sends remote arguments as one shell-inert word
        argv = decode_args(argv[1] if len(argv) > 1 else '')
    args = parser.parse_args(argv)
    if args.command == '_run':  # the launcher passes the dispatcher-side locations explicitly
        if args.state:
            os.environ['AGENT_BRIDGE_STATE'] = args.state
        if args.config:
            os.environ['AGENT_BRIDGE_CONFIG'] = args.config
    cfg = load_config()
    if args.command == 'status':
        cmd_status(args, cfg)
    elif args.command in ('start', 'send'):
        cmd_start(args, cfg, args.command)
    elif args.command == 'desktop':
        args.force = True
        cmd_start(args, cfg, 'desktop')
    elif args.command == 'job':
        if args.job_action != 'list' and not args.job:
            fail('job id required')
        cmd_job(args, cfg)
    elif args.command == 'notify':
        cmd_notify(args, cfg)
    elif args.command == '_run':
        run_job(args.job)


if __name__ == '__main__':
    main()
