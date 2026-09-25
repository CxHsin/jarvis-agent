import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from agent.agent import Agent
from tools.shell_sandbox import SandboxUnavailable, start_shell
from tests.test_tool_runtime_acceptance import Client, answer, call, config, tool_results


def _run_probe(workspace, protected, script, *, timeout=30):
    """Run a real restricted process, never an unsandboxed subprocess fallback."""
    probe = workspace / 'isolation-probe.py'
    probe.write_text(script, encoding='utf-8')
    with tempfile.TemporaryFile() as output:
        shell = start_shell(f"python -I '{probe}'", workspace, protected, output)
        try:
            shell.start()
            until = time.monotonic() + timeout
            while shell.poll() is None:
                assert time.monotonic() < until, 'Sandbox probe did not terminate'
                time.sleep(0.02)
            code = shell.poll()
        finally:
            shell.close()
        output.seek(0)
        assert code == 0, output.read().decode('utf-8', errors='replace')
    return json.loads((workspace / 'probe-result.json').read_text(encoding='utf-8'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_denies_outside_workspace_and_protected_state(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'secret.txt'
    secret.write_text('outside-secret', encoding='utf-8')
    protected = workspace / 'state'
    protected.mkdir()
    state_secret = protected / 'secret.txt'
    state_secret.write_text('state-secret', encoding='utf-8')
    script = '''import json
from pathlib import Path
outside = Path(%r)
state = Path(%r)
results = {}
for name, path in [('outside', outside), ('state', state)]:
    try:
        results[name + '_read'] = path.read_text() == name + '-secret'
    except OSError:
        results[name + '_read'] = False
    try:
        path.write_text('modified')
        results[name + '_write'] = True
    except OSError:
        results[name + '_write'] = False
Path('allowed.txt').write_text('allowed')
Path('probe-result.json').write_text(json.dumps(results))
''' % (str(secret), str(state_secret))
    result = _run_probe(workspace, protected, script)
    assert result == {'outside_read': False, 'outside_write': False,
                      'state_read': False, 'state_write': False}
    assert secret.read_text() == 'outside-secret'
    assert state_secret.read_text() == 'state-secret'
    assert (workspace / 'allowed.txt').read_text() == 'allowed'


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_cannot_connect_to_local_listener(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        script = '''import json, socket
from pathlib import Path
connected = False
try:
    with socket.create_connection(('127.0.0.1', %d), timeout=2):
        connected = True
except OSError:
    pass
Path('probe-result.json').write_text(json.dumps({'connected': connected}))
''' % port
        result = _run_probe(workspace, tmp_path / 'state', script)
        assert result == {'connected': False}
        with pytest.raises(socket.timeout):
            listener.accept()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_cannot_connect_to_host_non_loopback_listener(tmp_path):
    """Probe the host's LAN address without contacting an external service."""
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None,
                                                                     socket.AF_INET, socket.SOCK_STREAM)
                        if not item[4][0].startswith(('127.', '169.254.'))})
    if not addresses:
        pytest.skip('No host LAN address is available; non-loopback network boundary unverified')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with socket.socket() as listener:
        try:
            listener.bind((addresses[0], 0))
        except OSError:
            pytest.skip('Cannot bind a listener on the host LAN address')
        listener.listen(1)
        listener.settimeout(0.2)
        host, port = listener.getsockname()
        script = '''import json, socket
from pathlib import Path
connected = False
try:
    with socket.create_connection((%r, %d), timeout=2):
        connected = True
except OSError:
    pass
Path('probe-result.json').write_text(json.dumps({'connected': connected}))
''' % (host, port)
        assert _run_probe(workspace, tmp_path / 'state', script) == {'connected': False}
        with pytest.raises(socket.timeout):
            listener.accept()


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_link_swapped_after_scan_before_acl_grant_is_rejected(tmp_path, monkeypatch):
    import tools.shell_sandbox as sandbox
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('original', encoding='utf-8')
    target = workspace / 'target.txt'
    target.write_text('safe', encoding='utf-8')
    original_verify = sandbox._verify_tree
    swapped = False
    def swap_after_scan(root, protected, check):
        nonlocal swapped
        original_verify(root, protected, check)
        if root == workspace and not swapped:
            target.unlink()
            os.link(outside, target)
            swapped = True
    monkeypatch.setattr(sandbox, '_verify_tree', swap_after_scan)
    with tempfile.TemporaryFile() as output:
        with pytest.raises(SandboxUnavailable, match='link'):
            start_shell("[IO.File]::WriteAllText('target.txt','bad')", workspace,
                        tmp_path / 'state', output)
    assert swapped
    assert outside.read_text(encoding='utf-8') == 'original'


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_rejects_workspace_reparse_point_before_execution(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel.txt').write_text('original')
    try:
        (workspace / 'alias').symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(workspace / 'alias'), str(outside)],
                                capture_output=True, text=True)
        if result.returncode:
            pytest.skip(f'Cannot create directory junction on this host: {result.stderr}')
    with tempfile.TemporaryFile() as output:
        with pytest.raises(SandboxUnavailable, match='reparse point'):
            start_shell("[IO.File]::WriteAllText('ran.txt','bad')", workspace, tmp_path / 'state', output)
    assert not (workspace / 'ran.txt').exists()
    assert (outside / 'sentinel.txt').read_text() == 'original'


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_rejects_workspace_hardlink_before_execution(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('original')
    os.link(outside, workspace / 'alias.txt')
    with tempfile.TemporaryFile() as output:
        with pytest.raises(SandboxUnavailable, match='hardlinked file'):
            start_shell("[IO.File]::WriteAllText('alias.txt','bad')", workspace, tmp_path / 'state', output)
    assert outside.read_text() == 'original'


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer integration')
def test_shell_fails_closed_if_isolation_setup_fails(tmp_path, monkeypatch):
    import tools.shell_sandbox as sandbox
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    def fail_verify(*args):
        raise SandboxUnavailable('injected verification failure')
    monkeypatch.setattr(sandbox, '_verify_tree', fail_verify)
    with tempfile.TemporaryFile() as output:
        with pytest.raises(SandboxUnavailable, match='injected verification failure'):
            start_shell("[IO.File]::WriteAllText('ran.txt','bad')", workspace, tmp_path / 'state', output)
    assert not (workspace / 'ran.txt').exists()


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
