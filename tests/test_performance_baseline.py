"""Exercise the replay command against real temporary storage, offline."""
import json
from pathlib import Path
import subprocess
import sys


def test_replay_preserves_fact_provenance_and_history_window(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'result.json'
    completed = subprocess.run([
        sys.executable, str(root / 'benchmarks' / 'baseline.py'),
        '--target', str(root), '--output', str(output),
        '--scales', '3', '--repeats', '2', '--sections', 'memory,history',
    ], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    rows = report['measurements']
    for scenario in ('no-vector', 'rebuilding', 'ready'):
        queries = [row for row in rows if row['operation'] == 'memory_search'
                   and row['scenario'] == scenario]
        assert len(queries) == 2
        assert all('topic000000' in row['observed']['objects'] for row in queries)
        assert all(row['observed']['source_event'] == 'synthetic-event-0' for row in queries)
    history = [row for row in rows if row['operation'] == 'history_task']
    assert len(history) == 2
    assert all(row['observed']['history_has_probe'] for row in history)
    assert all(row['observed']['recent_has_answer'] for row in history)
    assert all(not row['observed']['history_has_answer'] for row in history)
    assert report['dataset']['version'] == 1
