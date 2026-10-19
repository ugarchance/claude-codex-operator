#!/usr/bin/env python3
"""Fake `claude`, `codex` and notify sink for agent-bridge tests (no model calls).

usage: fakes.py claude ARGS... | fakes.py codex ARGS... | fakes.py sink OUT_FILE
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path


def claude(args):
    if args[:1] == ['--version']:
        print('9.9.9 (Fake Claude)')
        return 0
    if args[:2] == ['agents', '--json']:
        print('[]')
        return 0
    if '--desktop' in args:
        print('Opening session %s in Claude Desktop' % args[args.index('--resume') + 1])
        return 0
    prompt = sys.stdin.buffer.read().decode('utf-8')  # the real CLIs read UTF-8 bytes on every OS
    session = args[args.index('--resume') + 1] if '--resume' in args else str(uuid.uuid4())
    if 'STALL' in prompt:  # silent at first, like a CLI blocked on a privacy prompt
        time.sleep(6)
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session, 'cwd': os.getcwd()}), flush=True)
    if 'SLEEP' in prompt:
        time.sleep(30)
    text = 'ok: ' + prompt.strip() + (' (resumed)' if '--resume' in args else '')
    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}, 'session_id': session}), flush=True)
    error = 'FAIL' in prompt
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': error, 'result': text, 'session_id': session,
                      'total_cost_usd': 0.01}), flush=True)
    return 1 if error else 0


def rollout_for(thread):
    root = Path(os.environ['CODEX_HOME']) / 'sessions' / '2026' / '10' / '19'
    root.mkdir(parents=True, exist_ok=True)
    return root / ('rollout-2026-10-19T00-00-00-%s.jsonl' % thread)


def codex(args):
    if args[:1] == ['--version']:
        print('codex-cli 9.9.9')
        return 0
    if args[:1] == ['queue']:
        if args[1:2] == ['--help']:
            print('Usage: codex queue [OPTIONS] --thread <THREAD> --message <TEXT>')
            return 0
        thread, message = args[args.index('--thread') + 1], args[args.index('--message') + 1]
        with open(str(rollout_for(thread)), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n')
            handle.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': 'queued ok: ' + message}}) + '\n')
        print('Queued message %s for thread %s.' % (uuid.uuid4(), thread))
        return 0
    if args[:1] == ['exec']:
        prompt = sys.stdin.buffer.read().decode('utf-8')  # the real CLIs read UTF-8 bytes on every OS
        thread = args[args.index('resume') + 1] if 'resume' in args else str(uuid.uuid4())
        out = args[args.index('-o') + 1]
        print(json.dumps({'type': 'thread.started', 'thread_id': thread}), flush=True)
        text = 'codex ok: ' + prompt.strip()
        print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}), flush=True)
        print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)
        Path(out).write_text(text, encoding='utf-8')
        with open(str(rollout_for(thread)), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'type': 'session_meta', 'payload': {'id': thread, 'cwd': os.getcwd()}}) + '\n')
        return 0
    return 2


def sink(args):
    with open(args[0], 'a', encoding='utf-8') as handle:
        handle.write(sys.stdin.buffer.read().decode('utf-8') + '\n')
    return 0


if __name__ == '__main__':
    sys.exit({'claude': claude, 'codex': codex, 'sink': sink}[sys.argv[1]](sys.argv[2:]))
