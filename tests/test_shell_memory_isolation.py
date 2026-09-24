import json
import os
import subprocess
import sys
import time

import pytest

from agent.agent import Agent
from tests.test_tool_runtime_acceptance import Client, answer, call, config, tool_results


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_can_write_workspace_but_cannot_overwrite_memory(tmp_path):
    settings = config(tmp_path, tool_permission_mode='broad-access')
    memory = settings.state_dir / 'memory'
    memory.mkdir(parents=True, exist_ok=True)
    sentinel = memory / 'sentinel.md'
    sentinel.write_text('original', encoding='utf-8')
    command = f"[IO.File]::WriteAllText('{tmp_path / 'allowed.txt'}','allowed'); [IO.File]::WriteAllText('{sentinel}','compromised')"
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
    command = f"python -I '{script}'"
    agent = Agent(settings, Client(answer(call('bash', {'command': command, 'timeout': 30})), {'content': 'done'}))
    try:
        agent.run_request('Run the isolation check')
        assert (tmp_path / 'probe-result.json').exists(), (tool_results(agent),
            (tmp_path / 'progress.txt').read_text() if (tmp_path / 'progress.txt').exists() else 'not started')
        assert json.loads((tmp_path / 'probe-result.json').read_text()) == {
            'sql': 'denied', 'read': 'denied', 'rename': 'denied', 'create': 'denied'}
        assert not (settings.state_dir / 'memory' / 'memory.db').exists()
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_python_bridge_preserves_windows_arguments(tmp_path):
    """The PowerShell bridge must preserve argv, including quotes and slashes."""
    script = tmp_path / 'argv probe.py'
    script.write_text(
        'import json, sys\nfrom pathlib import Path\n'
        "Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n",
        encoding='utf-8')
    args = ['space value', '鐪塵laut', 'quote"value', r'C:\\', '']
    rendered = ' '.join("'" + value.replace("'", "''") + "'" for value in args)
    command = f"python -I '{script}' {rendered} 123"
    agent = Agent(config(tmp_path, tool_permission_mode='broad-access'), Client(
        answer(call('bash', {'command': command})), {'content': 'done'}))
    try:
        agent.run_request('Run argv check')
        assert json.loads((tmp_path / 'argv.json').read_text()) == args + ['123']
        assert tool_results(agent)[0]['ok'] is True
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_deleting_granted_file_does_not_break_next_shell(tmp_path):
    (tmp_path / 'delete-me.txt').write_text('temporary')
    agent = Agent(config(tmp_path, tool_permission_mode='broad-access'), Client(
        answer(call('bash', {'command': f"[IO.File]::Delete('{tmp_path / 'delete-me.txt'}')"})),
        answer(call('bash', {'command': f"[IO.File]::WriteAllText('{tmp_path / 'next.txt'}','next')"}, call_id='second')), {'content': 'done'}))
    try:
        agent.run_request('Delete the temporary file and write the next file')
        assert not (tmp_path / 'delete-me.txt').exists()
        assert (tmp_path / 'next.txt').read_text().strip() == 'next'
        assert all(result['ok'] for result in tool_results(agent))
    finally:
        agent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
@pytest.mark.parametrize('creationflags', [subprocess.CREATE_BREAKAWAY_FROM_JOB if os.name == 'nt' else 0,
                                         subprocess.DETACHED_PROCESS if os.name == 'nt' else 0])
def test_child_remains_in_job_and_dies_on_shell_exit(tmp_path, creationflags):
    """Creation success does not prove breakaway; inspect the actual child."""
    import ctypes as c
    from ctypes import wintypes as w
    import tempfile
    from tools.shell_sandbox import start_shell

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    child_script = workspace / 'child.py'
    child_script.write_text(
        "import os, time\nfrom pathlib import Path\n"
        "Path('child-ready.tmp').write_text(str(os.getpid()))\nPath('child-ready.tmp').replace('child-ready')\ntime.sleep(60)\n"
        "Path('late.txt').write_text('bad')\n", encoding='utf-8')
    script = workspace / 'spawn.py'
    script.write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "try:\n"
        f"    subprocess.Popen([sys.executable, '-I', 'child.py'], creationflags={creationflags})\n"
        "except OSError as exc:\n"
        "    Path('rejected.tmp').write_text(str(exc.winerror))\n    Path('rejected.tmp').replace('rejected')\n"
        "until = time.monotonic() + 20\n"
        "while not Path('release').exists() and time.monotonic() < until: time.sleep(0.01)\n",
        encoding='utf-8')
    with tempfile.TemporaryFile() as output:
        shell = start_shell(f"python -I '{script}'", workspace, tmp_path / 'state', output)
        child_handle = None
        try:
            shell.start()
            until = time.monotonic() + 15
            while not any((workspace / name).exists() for name in ('child-ready', 'rejected')):
                assert shell.poll() is None, 'Shell exited before child reported its state'
                assert time.monotonic() < until, 'Child did not start'
                time.sleep(0.01)
            if (workspace / 'rejected').exists():
                assert creationflags == subprocess.CREATE_BREAKAWAY_FROM_JOB
                assert (workspace / 'rejected').read_text() == '5'  # ERROR_ACCESS_DENIED
            else:
                kernel = shell.kernel
                kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
                kernel.OpenProcess.restype = w.HANDLE
                kernel.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)]
                # Read the executing interpreter's PID, not a venv redirector's PID.
                pid = int((workspace / 'child-ready').read_text())
                child_handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
                assert child_handle
                member = w.BOOL()
                assert kernel.IsProcessInJob(child_handle, shell.job, c.byref(member))
                assert member.value, 'Child escaped the Jarvis job'
            (workspace / 'release').write_text('release')
            until = time.monotonic() + 10
            while shell.poll() is None:
                assert time.monotonic() < until, 'Shell did not exit'
                time.sleep(0.01)
            shell.close()
            if child_handle:
                assert shell.wait_for(child_handle, 5000) == 0, 'Child survived job cleanup'
            assert not (workspace / 'late.txt').exists()
        finally:
            shell.close()
            if child_handle:
                shell.close_handle(child_handle)


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
