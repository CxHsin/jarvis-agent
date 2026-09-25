"""Exercise event-log context projection against temporary storage."""
import json
from pathlib import Path
import subprocess
import sys


def test_replay_preserves_recent_context_and_source_identity(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'result.json'
    completed = subprocess.run([
        sys.executable, str(root / 'benchmarks' / 'baseline.py'),
        '--target', str(root), '--output', str(output),
        '--scales', '3', '--repeats', '2',
    ], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert len(report['measurements']) == 2
    for row in report['measurements']:
        assert row['operation'] == 'context_projection'
        assert row['observed']['messages'] == 3
        assert row['observed']['recent_has_answer']
        assert row['observed']['first_event_id'] == 'event-0'
    assert report['dataset']['version'] == 3
