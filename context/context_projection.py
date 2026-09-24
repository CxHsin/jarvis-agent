"""In-memory task folding for session context selection; the event log is authoritative."""

from copy import deepcopy
import json
from pathlib import Path


def read_events(path: Path, session_id: str = ''):
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # A torn tail is left untouched in the original log.
        if isinstance(event, dict):
            events.append(event)
    return events


class TaskHistory:
    """Rebuild recent task messages from committed events, never from Markdown."""

    def __init__(self, path: Path, count: int):
        self.path = path
        self.count = count
        self.tasks = []
        for event in read_events(path):
            self._apply(event)

    def _apply(self, event):
        if event.get('migration_projection') == 'session':
            return
        kind = event.get('type')
        if kind == 'task':
            if self.tasks and self.tasks[-1]['status'] == 'active':
                self.tasks[-1]['status'] = 'interrupted'
            self.tasks.append(dict(event, messages=[], events=[], status='active'))
        elif self.tasks and kind == 'message':
            self.tasks[-1]['messages'].append(event['message'])
        elif self.tasks and kind == 'task_end':
            self.tasks[-1]['status'] = event['status']
            self.tasks[-1]['context_valid'] = event.get('context_valid', event['status'] != 'failed')
        elif kind in {'context_reset', 'context_rollback'}:
            for task in self.tasks:
                if task.get('sequence', 0) > event['through_sequence']:
                    task['status'] = 'failed'
                    task['context_valid'] = False
        if self.tasks and event.get('task_id') == self.tasks[-1].get('task_id'):
            self.tasks[-1]['events'].append(event)

    def record(self, event):
        self._apply(event)

    def recent_tasks(self):
        finished = [task for task in self.tasks if task['status'] != 'active'
                    and task.get('context_valid', task['status'] != 'failed')]
        active = [task for task in self.tasks if task['status'] == 'active']
        return finished[-self.count:] + active

    def messages(self):
        return deepcopy([message for task in self.recent_tasks() for message in task['messages']])

    def message_event_ids(self):
        return [event['event_id'] for task in self.recent_tasks() for event in task['events']
                if event['type'] == 'message' and 'event_id' in event]
