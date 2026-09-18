import json
import os
import subprocess
import sys
import time

import pytest

from jarvis_agent import Agent
from tests.test_tool_runtime_acceptance import Client, answer, call, config, tool_results


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_can_write_workspace_but_cannot_overwrite_memory(tmp_path):
    settings = config(tmp_path, tool_permission_mode='broad-access')
    memory = settings.state_dir / 'memory'
    memory.mkdir(parents=True, exist_ok=True)
    sentinel = memory / 'sentinel.md'
    sentinel.write_text('original', encoding='utf-8')
    command = f'echo allowed>allowed.txt & echo compromised>"{sentinel}"'
    agent = Agent(settings, Client(answer(call('bash', {'command': command})), {'content': 'done'}))
    try:
        agent.run_request('Run the workspace command')
        assert (tmp_path / 'allowed.txt').read_text().strip() == 'allowed'
        assert sentinel.read_text(encoding='utf-8') == 'original'
        assert tool_results(agent)[0]['exit_code'] != 0
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_python_child_cannot_use_sql_or_replace_protected_directory(tmp_path):
    settings = config(tmp_path, tool_permission_mode='broad-access')
    script = tmp_path / 'probe.py'
    script.write_text('''import json, os, sqlite3
from pathlib import Path
state = Path('state')
results = {}
for name, action in [
    ('read', lambda: (state / 'memory' / 'memory.md').read_text()),
    ('rename', lambda: state.rename('moved-state')),
    ('create', lambda: (state / 'injected.txt').write_text('bad')),
    ('sql', lambda: sqlite3.connect(state / 'memory' / 'memory.db', timeout=0).execute('DROP TABLE memory_facts')),
]:
    try:
        Path('progress.txt').write_text(name)
        action()
        results[name] = 'allowed'
    except sqlite3.Error as exc:
        assert exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        results[name] = 'denied'
    except OSError:
        results[name] = 'denied'
Path('probe-result.json').write_text(json.dumps(results))
''', encoding='utf-8')
    command = f'"{sys._base_executable}" -I "{script}"'
    agent = Agent(settings, Client(answer(call('bash', {'command': command, 'timeout': 30})), {'content': 'done'}))
    try:
        agent.run_request('Run the isolation check')
        assert (tmp_path / 'probe-result.json').exists(), (tool_results(agent),
            (tmp_path / 'progress.txt').read_text() if (tmp_path / 'progress.txt').exists() else 'not started')
        assert json.loads((tmp_path / 'probe-result.json').read_text()) == {
            'sql': 'denied', 'read': 'denied', 'rename': 'denied', 'create': 'denied'}
        assert agent.store.memory.facts() == []
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_deleting_granted_file_does_not_break_next_shell(tmp_path):
    (tmp_path / 'delete-me.txt').write_text('temporary')
    agent = Agent(config(tmp_path, tool_permission_mode='broad-access'), Client(
        answer(call('bash', {'command': "Remove-Item -LiteralPath 'delete-me.txt'"})),
        answer(call('bash', {'command': "Set-Content -LiteralPath 'next.txt' -Value next"}, call_id='second')), {'content': 'done'}))
    try:
        agent.run_request('Delete the temporary file and write the next file')
        assert not (tmp_path / 'delete-me.txt').exists()
        assert (tmp_path / 'next.txt').read_text().strip() == 'next'
        assert all(result['ok'] for result in tool_results(agent))
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_detached_child_cannot_survive_shell_exit_or_break_out_of_job(tmp_path):
    script = tmp_path / 'spawn.py'
    script.write_text('''import json, subprocess, sys, time
from pathlib import Path
escaped = False
try:
    subprocess.Popen([sys.executable, '-I', '-c', 'pass'], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)
    escaped = True
except OSError:
    pass
child = subprocess.Popen([sys.executable, '-I', '-c',
    "from pathlib import Path; import time; Path('child-ready').write_text('ready'); time.sleep(3); Path('late.txt').write_text('bad')"],
    creationflags=subprocess.DETACHED_PROCESS)
until = time.monotonic() + 5
while not Path('child-ready').exists() and time.monotonic() < until:
    time.sleep(0.01)
Path('spawn-result.json').write_text(json.dumps({'escaped': escaped, 'ready': Path('child-ready').exists(), 'pid': child.pid}))
''', encoding='utf-8')
    agent = Agent(config(tmp_path, tool_permission_mode='broad-access'), Client(
        answer(call('bash', {'command': f'"{sys._base_executable}" -I "{script}"', 'timeout': 30})), {'content': 'done'}))
    try:
        agent.run_request('Run the child-process check')
        result = json.loads((tmp_path / 'spawn-result.json').read_text())
        assert result['escaped'] is False
        assert result['ready'] is True
        time.sleep(3.1)
        assert not (tmp_path / 'late.txt').exists()
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_existing_hardlink_cannot_bypass_memory_boundary(tmp_path):
    settings = config(tmp_path, tool_permission_mode='broad-access')
    memory = settings.state_dir / 'memory'
    memory.mkdir(parents=True)
    sentinel = memory / 'sentinel.md'
    sentinel.write_text('original')
    os.link(sentinel, tmp_path / 'alias.md')
    agent = Agent(settings, Client(answer(call('bash', {'command': 'echo changed>alias.md'})), {'content': 'done'}))
    try:
        agent.run_request('Run the command')
        assert sentinel.read_text() == 'original'
        assert tool_results(agent)[0]['ok'] is False
    finally:
        agent.close()
