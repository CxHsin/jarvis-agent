"""Immutable task evidence and rebuildable, session-local Recent views."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid


class TaskHistory:
    def __init__(self, directory: Path, session_id: str, count: int, memory=None):
        self.directory = directory
        self.session_id = session_id
        self.count = count
        self.memory = memory
        self.path = directory / "trajectories" / f"{session_id}.jsonl"
        self.recent_path = directory / "recent" / session_id / "recent.md"
        self.tasks = []
        self._needs_separator = False
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8", errors="replace")
            self._needs_separator = bool(text and not text.endswith("\n"))
            for index, line in enumerate(text.splitlines()):
                try:
                    event = json.loads(line)
                    event.setdefault("event_id", uuid.uuid5(uuid.NAMESPACE_URL, f"{session_id}:{index}:{line}").hex)
                    self._apply(event)
                except json.JSONDecodeError:
                    continue
        self._project()

    def _apply(self, event):
        kind = event["type"]
        if kind == "task":
            if self.tasks and self.tasks[-1]["status"] == "active":
                self.tasks[-1]["status"] = "interrupted"
            self.tasks.append(dict(event, messages=[], events=[], status="active"))
        elif self.tasks and kind == "message":
            self.tasks[-1]["messages"].append(event["message"])
        elif self.tasks and kind == "task_end":
            self.tasks[-1]["status"] = event["status"]
        if self.tasks and event.get("task_id") == self.tasks[-1]["task_id"]:
            self.tasks[-1]["events"].append(event)

    def record(self, record):
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        event = dict(record, recorded_at=now, event_id=uuid.uuid4().hex)
        event.setdefault("occurred_at", None)
        if event["type"] == "task":
            event["task_id"] = f"{self.session_id}:{uuid.uuid4().hex}"
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
        self._apply(event)
        if event["type"] == "task":
            history = self.directory / "history" / f"{now[:10]}.md"
            history.parent.mkdir(parents=True, exist_ok=True)
            # JSON quoting keeps each query on one auditable Markdown line.
            query = json.dumps(event["goal"], ensure_ascii=False)
            with history.open("a", encoding="utf-8") as handle:
                handle.write(f'- occurred_at={event["occurred_at"]} recorded_at={now} '
                             f'task={event["task_id"]} event={event["event_id"]} source={self.path.name}: {query}\n')
                handle.flush()
                os.fsync(handle.fileno())
        if event["type"] in {"task", "message", "task_end"}:
            self._project()

    def recent_tasks(self):
        completed = [task for task in self.tasks if task["status"] not in {"active", "failed"}]
        active = [task for task in self.tasks if task["status"] == "active"]
        return completed[-self.count:] + active

    def messages(self):
        return deepcopy([message for task in self.recent_tasks() for message in task["messages"]])

    def _project(self):
        if self.memory is not None:
            retained = {task["task_id"] for task in self.recent_tasks()}
            for task in self.tasks:
                if task["status"] == "completed" and task["task_id"] not in retained:
                    self.memory.enqueue(task, self.path)
        self.recent_path.parent.mkdir(parents=True, exist_ok=True)
        content = "# Recent\n\n"
        for task in self.recent_tasks():
            content += f'## {task["task_id"]} ({task["status"]})\n\n'
            content += json.dumps(task["messages"], ensure_ascii=False, indent=2) + "\n\n"
        temporary = self.recent_path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(self.recent_path)
