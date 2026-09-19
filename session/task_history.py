"""Immutable task evidence and rebuildable, session-local Recent views."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
import uuid


# Serialize shared daily files and session projections in the supported process.
_projection_lock = RLock()


def read_events(path, session_id):
    if not path.exists():
        return []
    events = []
    for index, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # A torn tail remains untouched in the original file.
        if not isinstance(event, dict):
            continue
        event.setdefault('event_id', uuid.uuid5(uuid.NAMESPACE_URL, f'{session_id}:{index}:{line}').hex)
        events.append(event)
    return events


def event_sources(directory):
    """Committed canonical streams, plus trajectories not yet migrated."""
    from session.session_migration import committed_path
    migrated = set()
    sources = []
    for original in sorted((directory.parent / 'sessions').glob('*/*.jsonl')):
        path = committed_path(original)
        if path != original:
            migrated.add(original.stem)
        events = read_events(path, original.stem)
        if events and events[0].get('version') == 2:
            sources.append((original.stem, path, events))
    for path in sorted((directory / 'trajectories').glob('*.jsonl')):
        if path.stem not in migrated:
            sources.append((path.stem, path, read_events(path, path.stem)))
    return sources


def project_history(directory):
    """Rebuild daily user-input indexes without consuming edited Markdown."""
    with _projection_lock:
        days = {}
        for _, path, events in event_sources(directory):
            for event in events:
                if event.get('type') != 'task' or event.get('migration_projection') == 'session':
                    continue
                now = event['recorded_at']
                query = json.dumps(event['goal'], ensure_ascii=False)
                line = (f'- occurred_at={event.get("occurred_at")} recorded_at={now} '
                        f'task={event["task_id"]} event={event["event_id"]} source={path}: {query}\n')
                days.setdefault(now[:10], []).append((now, event['event_id'], line))
        for day, rows in days.items():
            _publish(directory / 'history' / f'{day}.md', ''.join(row[2] for row in sorted(rows)))


def rebuild_projections(directory, memory):
    """Keep recovery's snapshot and publication in one foreground lock scope."""
    with _projection_lock:
        for session_id, path, _ in event_sources(directory):
            history = TaskHistory(directory, session_id, None, memory, event_path=path, publish=False)
            history.project(include_history=False)
        project_history(directory)


def _publish(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(path)


class TaskHistory:
    def __init__(self, directory: Path, session_id: str, count: int | None, memory=None, *,
                 event_path=None, publish=True):
        self.directory = directory
        self.session_id = session_id
        self.count = count
        self.memory = memory
        self.path = event_path or directory / "trajectories" / f"{session_id}.jsonl"
        self._shared_events = event_path is not None
        self.recent_path = directory / "recent" / session_id / "recent.md"
        self.tasks = []
        self._needs_separator = False
        with _projection_lock:
            for event in read_events(self.path, session_id):
                self._apply(event)
            if count is None:
                self.count = self.tasks[-1].get('recent_task_count', 5) if self.tasks else 5
            if self.path.exists() and not self._shared_events:
                self._needs_separator = not self.path.read_bytes().endswith(b'\n')
            if publish:
                self.project()

    def _apply(self, event):
        if event.get('migration_projection') == 'session':
            return
        kind = event["type"]
        if kind == "task":
            if self.tasks and self.tasks[-1]["status"] == "active":
                self.tasks[-1]["status"] = "interrupted"
            self.tasks.append(dict(event, messages=[], events=[], status="active"))
        elif self.tasks and kind == "message":
            self.tasks[-1]["messages"].append(event["message"])
        elif self.tasks and kind == "task_end":
            self.tasks[-1]["status"] = event["status"]
            self.tasks[-1]["context_valid"] = event.get("context_valid", event["status"] != "failed")
        elif kind in {"context_reset", "context_rollback"}:
            for task in self.tasks:
                if task.get("sequence", 0) > event["through_sequence"]:
                    task["status"] = "failed"
                    task["context_valid"] = False
        if self.tasks and event.get("task_id") == self.tasks[-1]["task_id"]:
            self.tasks[-1]["events"].append(event)

    def record(self, record):
        with _projection_lock:
            self._record(record)

    def _record(self, record):
        if self._shared_events:
            event = dict(record)
            now = event["recorded_at"]
        else:
            event = self._record_legacy(record)
            now = event["recorded_at"]
        self._apply(event)
        if event["type"] == "task" and not self._shared_events:
            history = self.directory / "history" / f"{now[:10]}.md"
            history.parent.mkdir(parents=True, exist_ok=True)
            # JSON quoting keeps each query on one auditable Markdown line.
            query = json.dumps(event["goal"], ensure_ascii=False)
            with history.open("a", encoding="utf-8") as handle:
                handle.write(f'- occurred_at={event["occurred_at"]} recorded_at={now} '
                             f'task={event["task_id"]} event={event["event_id"]} source={self.path.name}: {query}\n')
                handle.flush()
                os.fsync(handle.fileno())
        if event["type"] in {"task", "message", "task_end", "context_reset", "context_rollback"}:
            self.project(include_history=event['type'] == 'task')

    def _record_legacy(self, record):
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        event = dict(record, recorded_at=now, event_id=uuid.uuid4().hex)
        event.setdefault("occurred_at", None)
        if event["type"] == "task":
            event["task_id"] = f"{self.session_id}:{uuid.uuid4().hex}"
            event["recent_task_count"] = self.count
        elif self.tasks:
            event["task_id"] = self.tasks[-1]["task_id"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            if self._needs_separator:
                handle.write("\n")
                self._needs_separator = False
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def recent_tasks(self):
        retained_finished = [task for task in self.tasks if task["status"] != "active"
                             and task.get("context_valid", task["status"] != "failed")]
        active = [task for task in self.tasks if task["status"] == "active"]
        return retained_finished[-self.count:] + active

    def messages(self):
        return deepcopy([message for task in self.recent_tasks() for message in task["messages"]])

    def message_event_ids(self):
        return [event['event_id'] for task in self.recent_tasks() for event in task['events']
                if event['type'] == 'message']

    def project(self, *, include_history=True):
        with _projection_lock:
            self._project_locked(include_history)

    def _project_locked(self, include_history):
        if self._shared_events and include_history:
            project_history(self.directory)
        if self.memory is not None:
            retained = {task["task_id"] for task in self.recent_tasks()}
            for task in self.tasks:
                if task["status"] == "completed" and task["task_id"] not in retained:
                    self.memory.enqueue(task, self.path)
        content = "# Recent\n\n"
        for task in self.recent_tasks():
            content += f'## {task["task_id"]} ({task["status"]})\n\n'
            content += json.dumps(task["messages"], ensure_ascii=False, indent=2) + "\n\n"
        _publish(self.recent_path, content)
