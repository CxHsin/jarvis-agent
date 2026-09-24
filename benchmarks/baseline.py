"""Offline session context-projection baseline using temporary event logs."""
import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def revision(target):
    return subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()


def measure(target, scale, repeats):
    sys.path.insert(0, str(target))
    from context.context_projection import TaskHistory
    rows = []
    for _ in range(repeats):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            events = []
            for i in range(scale):
                task_id = f'session:{i}'
                events.extend([
                    dict(type='task', sequence=3*i+1, task_id=task_id, goal=f'probe-{i}'),
                    dict(type='message', sequence=3*i+2, task_id=task_id,
                         event_id=f'event-{i}', message=dict(role='assistant', content=f'answer-{i}')),
                    dict(type='task_end', sequence=3*i+3, task_id=task_id, status='completed'),
                ])
            path.write_text(''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')
            start = time.perf_counter()
            projection = TaskHistory(path, min(5, scale))
            messages = projection.messages()
            elapsed = (time.perf_counter() - start) * 1000
            rows.append(dict(operation='context_projection', scale=scale, elapsed_ms=elapsed,
                             observed=dict(messages=len(messages), recent_has_answer=
                                           messages[-1]['content'] == f'answer-{scale-1}',
                                           first_event_id=projection.message_event_ids()[0])))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scales', default='32')
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    target = args.target.resolve()
    scales = list(map(int, args.scales.split(',')))
    rows = [row for scale in scales for row in measure(target, scale, args.repeats)]
    report = dict(dataset=dict(version=3, scales=scales), repeats=args.repeats,
                  environment=dict(python=sys.version, platform=platform.platform(),
                                   target=str(target), commit=revision(target)), measurements=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
