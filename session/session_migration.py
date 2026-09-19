"""Validated, restartable imports; legacy evidence and Memory DB stay intact."""

import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def migration_directory(path):
    return path.parent / 'migrations' / path.stem


def committed_path(path):
    directory = migration_directory(path)
    marker = directory / 'committed.json'
    if not marker.exists():
        return path
    report = json.loads(marker.read_text(encoding='utf-8'))
    canonical = directory / 'events.jsonl'
    if report.get('version') != 1 or not canonical.is_file():
        raise ValueError('迁移提交标记或原始事件损坏；拒绝退回过期旧记录。')
    return canonical


def _write(path, data):
    with path.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _publish(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    _write(temporary, data)
    os.replace(temporary, path)


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')


def _backup_database(source_path, target_path):
    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(target_path)) as target:
        source.backup(target)
    with target_path.open('r+b') as handle:
        os.fsync(handle.fileno())


def protect_legacy_memory(config):
    """Capture existing personal state before MemoryService can recover/publish it."""
    from session.session_store import _first_record, _SessionLock, resolve_state_dir
    state = resolve_state_dir(config)
    if not any((_first_record(path) or {}).get('version', 1) == 1
               for path in (state / 'sessions').glob('*/*.jsonl')):
        return
    database = state / 'memory' / 'memory.db'
    if not database.exists():
        return
    destination = state / 'migrations' / 'legacy-memory'
    lock = _SessionLock(destination.parent / 'legacy-memory.lock')
    lock.acquire()
    try:
        if destination.exists():
            return
        staging = destination.with_suffix('.tmp')
        staging.mkdir(parents=True, exist_ok=True)
        _backup_database(database, staging / 'memory.db')
        for name in ('memory.md', 'self.md'):
            path = database.parent / name
            if path.exists():
                _write(staging / name, path.read_bytes())
        os.replace(staging, destination)
    finally:
        lock.release()


def _sources(path):
    data = path.read_bytes()
    result, warnings = [], []
    lines = data.splitlines()
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            line = raw.decode('utf-8')
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if index == len(lines) - 1 and not data.endswith(b'\n'):
                warnings.append(f'{path.name}:{index + 1}: 截断尾记录已保留在备份。')
                continue
            raise ValueError(f'{path}:{index + 1}: 无法解析旧记录，拒绝切换。')
        if not isinstance(record, dict) or 'type' not in record:
            raise ValueError(f'{path}:{index + 1}: 无效旧记录，拒绝切换。')
        result.append((index + 1, line, record))
    return result, warnings


def _convert(store, original, trajectory):
    events, mapping, warnings, conflicts = [], [], [], []
    seen = set()
    # The immutable backup's file timestamp is the persisted import receipt
    # time. Rebuilding an interrupted candidate must not invent another time.
    snapshot = migration_directory(original) / 'backup' / 'session.jsonl'
    received_at = datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc).isoformat(timespec='microseconds')
    for role, path in [('session', original), ('trajectory', trajectory)]:
        if not path.exists():
            continue
        sources, notices = _sources(path)
        warnings.extend(notices)
        task_id = None
        for line, raw, record in sources:
            identity = record.get('event_id') or uuid.uuid5(
                uuid.NAMESPACE_URL, f'{store.session_id}:{line - 1}:{raw}' if role == 'trajectory'
                else f'{store.session_id}:session:{line}').hex
            if identity in seen:
                conflicts.append(dict(reason='duplicate_event_identity', source=str(path), line=line,
                                      legacy_event_id=identity))
                identity = uuid.uuid5(uuid.NAMESPACE_URL, f'{path}:{line}:{identity}').hex
            seen.add(identity)
            if record['type'] == 'task':
                task_id = record.get('task_id') or f'{store.session_id}:{identity}'
            source = dict(path=str(path), line=line, event_id=record.get('event_id') or identity,
                          task_id=record.get('task_id'), recorded_at=record.get('recorded_at'))
            recorded_at = received_at
            if record.get('recorded_at'):
                try:
                    moment = datetime.fromisoformat(record['recorded_at'])
                    if moment.tzinfo is not None:
                        recorded_at = moment.astimezone(timezone.utc).isoformat(timespec='microseconds')
                except (TypeError, ValueError):
                    warnings.append(f'{path.name}:{line}: 旧记录时间无法验证，原值保留在来源中。')
            event = dict(record, event_version=1, event_id=identity, session_id=store.session_id,
                         sequence=len(events) + 1, task_id=record.get('task_id', task_id),
                         recorded_at=recorded_at,
                         occurred_at=record.get('occurred_at'), legacy_source=source,
                         migration_projection=role if trajectory.exists() else 'both')
            if role == 'session' and record['type'] == 'session':
                event['version'] = 2
            events.append(event)
            mapping.append(dict(source=source, event_id=identity, sequence=event['sequence']))
    if trajectory.exists():
        # Independent writes have no shared identity: even equal text cannot
        # prove the same occurrence. Retain both streams and their semantics.
        conflicts.append(dict(reason='independent_legacy_streams',
                              session=str(original), trajectory=str(trajectory),
                              resolution='preserved_both_without_text_deduplication'))
    return events, dict(version=1, sources=mapping, warnings=warnings, conflicts=conflicts)


def _validate_memory_sources(database, report):
    if not database.exists():
        return
    available = {(item['source']['path'], item['source']['task_id'], item['source']['event_id'])
                 for item in report['sources']}
    imported_paths = {item[0] for item in available}
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('fact_sources', 'pending_sources', 'pending_batches'):
            if table not in tables:
                continue
            for row in db.execute(f'SELECT * FROM {table}'):
                path = row['trajectory_path']
                if not path:  # User operations/profile edits are evidence in their own right.
                    continue
                identifiers = json.loads(row['source_event_ids']) if table == 'pending_batches' else [row['source_event_id']]
                for identity in identifiers:
                    if path in imported_paths:
                        resolved = (path, row['source_task_id'], identity) in available
                    else:
                        # Other sessions remain at their original locations; do
                        # not rewrite their references during this session import.
                        resolved = Path(path).is_file()
                    if not resolved:
                        report['conflicts'].append(dict(reason='unresolved_memory_source', table=table,
                            path=path, task_id=row['source_task_id'], event_id=identity,
                            resolution='original_reference_and_fact_preserved'))


def _validate(store, original, candidate, events):
    from session.session_store import SessionStore
    recovered = []
    for path in (original, candidate):
        reader = SessionStore(store.config, store.session_id, store.started_at)
        reader.path = path
        contents = reader.load()
        contents.warnings = ()
        recovered.append(contents)
    if recovered[0] != recovered[1]:
        raise ValueError('迁移改变恢复上下文，拒绝切换。')
    reread = [json.loads(line) for line in candidate.read_text(encoding='utf-8').splitlines()]
    if reread != events or len({event['event_id'] for event in events}) != len(events):
        raise ValueError('迁移事件身份或顺序校验失败。')
    for index, event in enumerate(events, 1):
        if event['sequence'] != index:
            raise ValueError('迁移事件顺序校验失败。')


def migrate(store):
    original = store.path
    canonical = committed_path(original)
    if canonical != original:
        return canonical
    directory = migration_directory(original)
    directory.mkdir(parents=True, exist_ok=True)
    trajectory = store.memory_directory / 'trajectories' / original.name
    backup = directory / 'backup'
    if not backup.exists():
        staging = directory / 'backup.tmp'
        staging.mkdir(exist_ok=True)
        _write(staging / 'session.jsonl', original.read_bytes())
        if trajectory.exists():
            _write(staging / 'trajectory.jsonl', trajectory.read_bytes())
        database = store.memory_directory / 'memory.db'
        if database.exists():
            _backup_database(database, staging / 'memory.db')
        os.replace(staging, backup)
    if original.read_bytes() != (backup / 'session.jsonl').read_bytes():
        raise ValueError('旧会话在迁移期间发生变化；保留备份，拒绝切换。')
    if trajectory.exists() != (backup / 'trajectory.jsonl').exists() or (
            trajectory.exists() and trajectory.read_bytes() != (backup / 'trajectory.jsonl').read_bytes()):
        raise ValueError('旧轨迹在迁移期间发生变化；保留备份，拒绝切换。')
    events, report = _convert(store, original, trajectory)
    candidate = directory / 'events.jsonl'
    _publish(candidate, b''.join(_encode(event) for event in events))
    _validate(store, original, candidate, events)
    _validate_memory_sources(backup / 'memory.db', report)
    _publish(directory / 'committed.json', _encode(report))
    for notice in report['warnings'] + [str(conflict) for conflict in report['conflicts']]:
        print(f'[会话迁移] {notice}', file=sys.stderr)
    return candidate
