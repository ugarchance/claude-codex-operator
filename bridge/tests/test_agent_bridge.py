"""Lifecycle tests for agent_bridge.py with fake CLIs (no model calls, no desktop apps).

Run: python3 bridge/tests/test_agent_bridge.py
Set AGENT_BRIDGE_TEST_LAUNCHER=launchd (a logged-in Mac) or =task (Windows with a desktop logon) to exercise the
desktop-session launchers instead of the direct one.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / 'agent_bridge.py'
FAKES = HERE / 'fakes.py'
DISPATCH = HERE.parent / 'dispatch.py'
RUNNER_C = HERE.parent / 'runner.c'
LAUNCHD = sys.platform == 'darwin' and os.environ.get('AGENT_BRIDGE_TEST_LAUNCHER') == 'launchd'


def has_compiler():
    return sys.platform == 'darwin' and subprocess.run(['xcode-select', '-p'], capture_output=True).returncode == 0


def build_runner(path):
    subprocess.run(['cc', '-O2', '-Wall', '-o', str(path), str(RUNNER_C)], check=True)
    return path


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='agent-bridge-test-'))
        self.sink = self.root / 'notify.jsonl'
        self.project = self.root / 'proje'
        self.project.mkdir()
        launcher = os.environ.get('AGENT_BRIDGE_TEST_LAUNCHER', 'direct')
        # desktop-session launchers run outside this shell's privacy grants: use copies outside ~/Documents etc.
        (self.root / 'bin').mkdir()
        self.bridge_path = Path(shutil.copy(str(BRIDGE), str(self.root / 'bin'))) if launcher != 'direct' else BRIDGE
        fakes = Path(shutil.copy(str(FAKES), str(self.root / 'bin'))) if launcher != 'direct' else FAKES
        cfg = {'machine': 'test', 'language': 'tr', 'claude': [sys.executable, str(fakes), 'claude'],
               'codex': [sys.executable, str(fakes), 'codex'], 'notify_command': [sys.executable, str(fakes), 'sink', str(self.sink)],
               'path': os.environ.get('PATH', '')}
        (self.root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        self.env = dict(os.environ, AGENT_BRIDGE_STATE=str(self.root / 'state'), AGENT_BRIDGE_CONFIG=str(self.root / 'config.json'),
                        CODEX_HOME=str(self.root / 'codex'), AGENT_BRIDGE_NO_OPEN='1',
                        AGENT_BRIDGE_LAUNCHER=launcher)
        self.env.pop('AGENT_BRIDGE_JOB', None)

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def bridge(self, *args, stdin=None, ok=True):
        result = subprocess.run([sys.executable, str(self.bridge_path)] + list(args), input=stdin, capture_output=True,
                                encoding='utf-8', env=self.env, timeout=120)
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(data.get('ok'), data)
        return data

    def wait(self, job):
        return self.bridge('job', 'wait', job, '--timeout', '60')

    def notes(self):
        return [json.loads(line) for line in self.sink.read_text(encoding='utf-8').splitlines()] if self.sink.exists() else []

    def test_claude_start_desktop_notify_then_follow_up(self):
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'merhaba', '--desktop', '--notify')['job']
        status = self.wait(job)
        self.assertEqual(status['state'], 'done', status)
        self.assertTrue(status['session'])
        self.assertTrue(status['desktop'].startswith('moved'), status)
        self.assertEqual(status['notify'], 'sent')
        self.assertTrue(status['finished'])
        note = self.notes()[-1]
        self.assertIn('ok: merhaba', note['message'])
        self.assertIn('[test · Claude] proje: bitti', note['message'])
        self.assertEqual(note['job'], job)
        self.assertEqual(self.bridge('job', 'result', job)['result'], 'ok: merhaba')
        follow = self.bridge('send', 'claude', '--session', status['session'], '--prompt', 'devam', '--notify')['job']
        status2 = self.wait(follow)
        self.assertEqual(status2['state'], 'done', status2)
        self.assertEqual(status2['session'], status['session'])
        self.assertEqual(status2['cwd'], os.path.realpath(str(self.project)))
        self.assertIn('(resumed)', self.bridge('job', 'result', follow)['result'])

    def test_claude_failure_is_reported(self):
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'FAIL please', '--notify')['job']
        status = self.wait(job)
        self.assertEqual(status['state'], 'failed', status)
        self.assertIn('başarısız', self.notes()[-1]['message'])

    def test_timeout_kills_the_agent(self):
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'SLEEP', '--timeout-h', '0.001')['job']
        status = self.wait(job)
        self.assertEqual(status['state'], 'failed', status)
        self.assertNotEqual(status.get('exit_code'), 0)

    def test_cancel_stops_a_running_agent(self):
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'SLEEP', '--notify')['job']
        deadline = time.time() + 60
        while not self.bridge('job', 'show', job).get('session') and time.time() < deadline:
            time.sleep(0.5)
        started = time.time()
        status = self.bridge('job', 'cancel', job)
        self.assertEqual(status['state'], 'cancelled', status)
        self.assertTrue(status['finished'])
        self.assertLess(time.time() - started, 25)  # the fake agent would sleep for 30 seconds
        self.assertIn('iptal edildi', self.notes()[-1]['message'])
        self.assertIn('already finished', self.bridge('job', 'cancel', job, ok=False)['error'])

    def test_codex_exec_then_queue_into_desktop_thread(self):
        job = self.bridge('start', 'codex', '--cwd', str(self.project), '--prompt', 'görev', '--notify')['job']
        status = self.wait(job)
        self.assertEqual(status['state'], 'done', status)
        thread = status['session']
        self.assertEqual(self.bridge('job', 'result', job)['result'], 'codex ok: görev')
        self.assertIn('codex://threads/' + thread, self.notes()[-1]['message'])
        follow = self.bridge('send', 'codex', '--session', thread, '--prompt', 'ikinci')['job']
        status2 = self.wait(follow)
        self.assertEqual(status2['state'], 'done', status2)
        self.assertEqual(status2['cwd'], os.path.realpath(str(self.project)))
        self.assertEqual(self.bridge('job', 'result', follow)['result'], 'queued ok: ikinci')

    def test_notify_question_from_inside_a_job(self):
        self.env['AGENT_BRIDGE_JOB'] = '20261019-000000-abcd'
        self.bridge('notify', '--prompt', 'Hangi ortamı kullanayım?')
        note = self.notes()[-1]
        self.assertEqual(note['kind'], 'question')
        self.assertIn('soru', note['message'])
        self.assertIn('Hangi ortamı', note['message'])

    def test_rejects_missing_cwd_and_lists_jobs(self):
        data = self.bridge('start', 'claude', '--cwd', str(self.root / 'yok'), '--prompt', 'x', ok=False)
        self.assertFalse(data['ok'])
        self.assertIn('cwd', data['error'])
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'liste')['job']
        self.wait(job)
        self.assertIn(job, [row['job'] for row in self.bridge('job', 'list')['jobs']])

    def test_rejects_unsafe_session_ids_and_bypass_mode(self):
        bad = self.bridge('send', 'claude', '--session', 'abc & calc', '--prompt', 'x', ok=False)
        self.assertEqual(bad['error'], 'invalid session id')
        bad = self.bridge('desktop', 'claude', '--session', '../../etc', ok=False)
        self.assertEqual(bad['error'], 'invalid session id')
        denied = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'x',
                             '--permission-mode', 'bypassPermissions', ok=False)
        self.assertIn('not allowed', denied['error'])
        cfg = json.loads((self.root / 'config.json').read_text(encoding='utf-8'))
        cfg['allow_bypass_permissions'] = True
        (self.root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'x',
                          '--permission-mode', 'bypassPermissions')['job']
        self.assertEqual(self.wait(job)['state'], 'done')

    def test_unicode_folder_prompt_and_single_worker(self):
        project = self.root / '🏰 Ürün çalışması'
        project.mkdir()
        job = self.bridge('start', 'claude', '--cwd', str(project), '--prompt-file', '-', stdin='şunu yap: “özet” 🙂')['job']
        status = self.wait(job)
        self.assertEqual(status['state'], 'done', status)
        self.assertEqual(status['cwd'], os.path.realpath(str(project)))
        self.assertEqual(self.bridge('job', 'result', job)['result'], 'ok: şunu yap: “özet” 🙂')
        ended = status['ended']
        subprocess.run([sys.executable, str(self.bridge_path), '_run', job], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(self.bridge('job', 'show', job)['ended'], ended)  # a second worker must not rerun the job

    def test_dispatch_forwards_prompt_through_stdin(self):
        machines = self.root / 'machines.json'
        machines.write_text(json.dumps({'routing': 'mobil işler: here', 'machines': {'here': {
            'local': True, 'python': sys.executable, 'bridge': str(self.bridge_path)}}}), encoding='utf-8')
        env = dict(self.env, AGENT_BRIDGE_MACHINES=str(machines))
        env.pop('MSYSTEM', None)
        prompt = 'uzun "tırnaklı" & $(echo kaçış) metin; ñ 🙂'

        def dispatch(*args, stdin=None):
            result = subprocess.run([sys.executable, str(DISPATCH)] + list(args), input=stdin, capture_output=True,
                                    encoding='utf-8', env=env, timeout=120)
            return json.loads(result.stdout)
        job = dispatch('here', 'start', 'claude', '--cwd', str(self.project), '--prompt', prompt)['job']
        self.assertEqual(dispatch('here', 'job', 'wait', job, '--timeout', '60')['state'], 'done')
        self.assertEqual(dispatch('here', 'job', 'result', job)['result'], 'ok: ' + prompt)
        job = dispatch('here', 'start', 'codex', '--cwd', str(self.project), stdin='stdin ile görev')['job']
        self.assertEqual(dispatch('here', 'job', 'wait', job, '--timeout', '60')['state'], 'done')
        self.assertEqual(dispatch('here', 'job', 'result', job)['result'], 'codex ok: stdin ile görev')
        self.assertEqual(dispatch('machines')['routing'], 'mobil işler: here')
        self.assertEqual(dispatch('status')['here']['machine'], 'test')
        self.assertFalse(dispatch('nowhere', 'status')['ok'])

    def test_dispatch_quotes_remote_words(self):
        sys.path.insert(0, str(DISPATCH.parent))
        import dispatch
        self.assertEqual(dispatch.posix_word('~/Desktop/a b'), '"$HOME"\'/Desktop/a b\'')
        self.assertEqual(dispatch.posix_word("it's"), "'it'\"'\"'s'")
        self.assertEqual(dispatch.powershell_word("it's"), "'it''s'")
        args, prompt = dispatch.split_prompt(['start', 'claude', '--prompt=merhaba', '--cwd', 'x'])
        self.assertEqual((args, prompt), (['start', 'claude', '--cwd', 'x'], 'merhaba'))

    def test_status_reports_fake_clis(self):
        info = self.bridge('status')
        self.assertEqual(info['machine'], 'test')
        self.assertIn('Fake Claude', info['claude']['version'])
        self.assertTrue(info['codex']['queue'])
        if sys.platform == 'darwin':
            self.assertIn('full_disk_access', info['runner'])

    def set_config(self, **values):
        cfg = json.loads((self.root / 'config.json').read_text(encoding='utf-8'))
        cfg.update(values)
        (self.root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')

    @unittest.skipUnless(LAUNCHD and has_compiler(), 'macOS launchd launcher and developer tools')
    def test_agents_run_behind_the_runner(self):
        runner = build_runner(self.root / 'bin' / 'agent-bridge-runner')
        self.set_config(mac_runner=str(runner))
        # auto: a freshly built runner holds no Full Disk Access, so the agent runs on its own
        status = self.wait(self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'tek')['job'])
        self.assertEqual(status['state'], 'done', status)
        self.assertIs(status['runner_access'], False)
        self.assertIsNone(status['runner'])
        self.assertIs(self.bridge('status')['runner']['full_disk_access'], False)
        self.set_config(mac_runner=str(runner), mac_runner_mode='always')
        job = self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'arkada', '--desktop')['job']
        status = self.wait(job)
        self.assertEqual(status['state'], 'done', status)
        self.assertEqual(status['runner'], str(runner))
        self.assertTrue(status['desktop'].startswith('moved'), status)
        for name in ('agent.plist', 'desktop.plist'):
            plist = plistlib.loads((self.root / 'state' / 'jobs' / job / name).read_bytes())
            self.assertEqual(plist['ProgramArguments'][0], str(runner))
        self.assertEqual(self.bridge('job', 'result', job)['result'], 'ok: arkada')

    @unittest.skipUnless(LAUNCHD, 'macOS launchd launcher')
    def test_silent_agent_is_reported_once(self):
        self.set_config(stall_warning_s=2)
        status = self.wait(self.bridge('start', 'claude', '--cwd', str(self.project), '--prompt', 'STALL', '--notify')['job'])
        self.assertEqual(status['state'], 'done', status)
        self.assertIn('çıktı yok', status['warning'])
        notes = self.notes()
        self.assertEqual([note['kind'] for note in notes], ['warning', 'result'])
        self.assertIn('proje: bekliyor', notes[0]['message'])


@unittest.skipUnless(has_compiler(), 'macOS developer tools')
class RunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix='agent-bridge-runner-'))
        cls.runner = str(build_runner(cls.dir / 'agent-bridge-runner'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.dir), ignore_errors=True)

    def run_runner(self, *args):
        return subprocess.run([self.runner] + list(args), capture_output=True, text=True, timeout=30)

    def test_exit_status_output_and_signals(self):
        result = self.run_runner('/bin/sh', '-c', 'echo out; echo err >&2; exit 3')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, 'out\n', 'err\n'))
        self.assertEqual(self.run_runner('/bin/sh', '-c', 'kill -TERM $$').returncode, -15)
        self.assertEqual(self.run_runner('/nonexistent').returncode, 127)
        self.assertEqual(self.run_runner().returncode, 64)
        self.assertIn(self.run_runner('--check').returncode, (0, 1))

    def test_forwards_termination_to_the_agent(self):
        marker = self.dir / 'child.pid'
        proc = subprocess.Popen([self.runner, '/bin/sh', '-c', 'echo $$ > "$0"; exec /bin/sleep 30', str(marker)])
        deadline = time.time() + 10
        while not (marker.exists() and marker.read_text().strip()) and time.time() < deadline:
            time.sleep(0.1)
        child = int(marker.read_text())
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), -15)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
