"""Temporal stable USER facts and auditable consolidation decisions."""

from datetime import datetime, timezone
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Callable
from sqlite3 import Connection

from memory_store import MemoryStore


CATEGORIES = {'identity', 'work_preferences', 'communication', 'long_term_goals',
              'constraints', 'current_state'}
# Only these well-defined single-valued attributes admit automatic supersession.
EXCLUSIVE_PREDICATES = {'preferred_name', 'primary_residence', 'current_employer',
                        'current_role', 'timezone', 'preferred_language'}


def timestamp(value=None):
    moment = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('Memory timestamps require a timezone')
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds')


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


class StableMemory:
    """Fact rules; mutations receive the caller-owned SQLite transaction."""

    def __init__(self, store: MemoryStore, index_fact: Callable[[Connection, str], None]):
        self.store = store
        self.index_fact = index_fact

    def initialize(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            object TEXT NOT NULL, object_normalized TEXT NOT NULL, text TEXT NOT NULL,
            category TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL,
            importance REAL NOT NULL, occurred_at TEXT, recorded_at TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, source_kind TEXT NOT NULL,
            invalidated_by TEXT, invalidation_reason TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS fact_sources (
            fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id), source_task_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL, trajectory_path TEXT NOT NULL, quote TEXT NOT NULL,
            recorded_at TEXT NOT NULL, occurred_at TEXT, source_kind TEXT NOT NULL,
            PRIMARY KEY(fact_id, source_task_id, source_event_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_conflicts (
            decision_id TEXT PRIMARY KEY, candidate_id TEXT, old_fact_id TEXT,
            new_fact_id TEXT, decision TEXT NOT NULL, reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_operations (
            operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT,
            recorded_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_schedule (
            name TEXT PRIMARY KEY, completed_day TEXT, lease_until TEXT, token TEXT)''')
        db.execute("INSERT OR IGNORE INTO memory_schedule(name) VALUES ('nightly')")
        db.execute('CREATE INDEX IF NOT EXISTS facts_attribute_time ON memory_facts(subject, predicate, valid_from)')
    def facts(self, include_inactive=False):
        """Read facts with immutable sources; history is opt-in."""
        with self.store.read() as db:
            now = timestamp()
            rows = [dict(row) for row in db.execute('SELECT * FROM memory_facts ' +
                    ('' if include_inactive else "WHERE status!='forgotten' AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ") +
                    'ORDER BY valid_from, fact_id', () if include_inactive else (now, now))]
            for row in rows:
                row['sources'] = [dict(source) for source in db.execute(
                    'SELECT * FROM fact_sources WHERE fact_id=? ORDER BY recorded_at, source_event_id',
                    (row['fact_id'],))]
        return rows

    def conflicts(self):
        with self.store.read() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM memory_conflicts ORDER BY recorded_at, decision_id')]

    def record_decision(self, db, candidate_id, old_id, new_id, decision, reason):
        key = hashlib.sha256(json.dumps([candidate_id, old_id, new_id, decision]).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO memory_conflicts VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (key, candidate_id, old_id, new_id, decision, reason, timestamp()))

    def validate(self, value):
        result = dict(value)
        if result.get('subject') != 'USER':
            raise ValueError('Only USER facts are eligible')
        for key in ('predicate', 'object'):
            if not isinstance(result.get(key), str) or not result[key].strip():
                raise ValueError(f'Fact requires {key}')
        result['predicate'] = normalized(result['predicate'])
        result['object'] = result['object'].strip()
        result['object_normalized'] = normalized(result['object'])
        if result.get('category') not in CATEGORIES:
            raise ValueError('Unknown user profile category')
        for key, default in [('confidence', 1.0), ('importance', 0.5)]:
            number = result.get(key, default)
            if not isinstance(number, (int, float)) or not 0 <= number <= 1:
                raise ValueError(f'{key} must be between zero and one')
            result[key] = number
        result['occurred_at'] = timestamp(result['occurred_at']) if result.get('occurred_at') else None
        result['text'] = result.get('candidate_text') or result.get('text') or result['object']
        return result

    def admission_reason(self, value):
        if value.get('inference') is not False or value.get('conflict') not in {'none', 'factual'}:
            return 'Inference or unresolved conflict requires review'
        if value.get('explicit') is not True or value.get('stable') is not True:
            return 'Only explicit stable user statements can be promoted'
        if value.get('sensitive') is not False and value.get('remember_consent') is not True:
            return 'Sensitive facts require explicit remember consent'
        if value.get('confidence', 0) < 0.8:
            return 'Insufficient confidence for automatic promotion'
        return None

    def promote_classified(self, db: Connection):
        """Apply admission and correction priority within the promotion transaction."""
        for row in list(db.execute("SELECT * FROM pending_candidates WHERE status='pending' AND classification IS NOT NULL")):
            value = json.loads(row['classification'])
            reason = self.admission_reason(value)
            old_id = None
            if not reason:
                value = self.validate(value)
                for old in db.execute("SELECT * FROM memory_facts WHERE subject=? AND predicate=? AND status='active'",
                                      (value['subject'], value['predicate'])):
                    if old['object_normalized'] == value['object_normalized']:
                        continue
                    if (old['source_kind'] == 'user_correction' and value['predicate'] in EXCLUSIVE_PREDICATES
                            and (value['occurred_at'] or row['recorded_at']) >= old['valid_from']):
                        reason, old_id = 'Existing user correction takes priority; requires review', old['fact_id']
                    elif value.get('conflict') == 'factual' and value['predicate'] not in EXCLUSIVE_PREDICATES:
                        reason, old_id = 'Unknown exclusivity requires factual conflict review', old['fact_id']
            if reason:
                db.execute('UPDATE pending_candidates SET reason=? WHERE candidate_id=?', (reason, row['candidate_id']))
                self.record_decision(db, row['candidate_id'], old_id, None, 'pending', reason)
                continue
            sources = [dict(source) for source in db.execute(
                'SELECT * FROM pending_sources WHERE candidate_id=?', (row['candidate_id'],))]
            fact_id = self.write_fact(db, self.validate(value), sources, 'user_statement', row['candidate_id'])
            db.execute('UPDATE pending_candidates SET status=?, reason=? WHERE candidate_id=?',
                       ('promoted' if fact_id else 'suppressed',
                        f'Consolidated into {fact_id}' if fact_id else 'Previously forgotten evidence', row['candidate_id']))

    def write_fact(self, db: Connection, value, sources, kind, candidate_id=None, correction_target=None):
        received = min(source['recorded_at'] for source in sources)
        start = value['occurred_at'] or received
        now = timestamp()
        matches = list(db.execute('SELECT * FROM memory_facts WHERE subject=? AND predicate=?',
                                 (value['subject'], value['predicate'])))
        # Explicit corrections and forgetting retire the assertion's old evidence,
        # including multi-valued predicates. New user evidence can establish it again.
        if kind == 'user_statement':
            for old in matches:
                retired = old['status'] == 'forgotten' or (
                    old['status'] == 'invalidated' and 'correction' in (old['invalidation_reason'] or '').lower())
                if retired and old['object_normalized'] == value['object_normalized']:
                    if max(source['recorded_at'] for source in sources) <= old['updated_at']:
                        self.record_decision(db, candidate_id, old['fact_id'], None, 'suppressed', 'Retired evidence cannot resurrect a fact')
                        return None
        duplicate = next((old for old in matches if old['status'] == 'active' and
                          old['object_normalized'] == value['object_normalized'] and
                          (value['predicate'] not in EXCLUSIVE_PREDICATES or start >= old['valid_from'])), None)
        if duplicate:
            fact_id = duplicate['fact_id']
            db.execute('UPDATE memory_facts SET recorded_at=MIN(recorded_at, ?), valid_from=MIN(valid_from, ?), updated_at=? WHERE fact_id=?',
                       (received, start, now, fact_id))
            if kind == 'user_correction':
                db.execute("UPDATE memory_facts SET source_kind='user_correction', confidence=1.0 WHERE fact_id=?", (fact_id,))
        else:
            fact_id = uuid.uuid4().hex
            db.execute('''INSERT INTO memory_facts VALUES
                (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)''',
                (fact_id, value['subject'], value['predicate'], value['object'], value['object_normalized'],
                 value['text'], value['category'], value['confidence'], value['importance'],
                 value['occurred_at'], received, start, kind, now, now))
        for source in sources:
            db.execute('INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (fact_id, source['source_task_id'], source['source_event_id'], source['trajectory_path'],
                        source['quote'], source['recorded_at'], source.get('occurred_at'), kind))
        self.index_fact(db, fact_id)
        if not duplicate and value['predicate'] in EXCLUSIVE_PREDICATES:
            # Insert into the effective-time timeline, including late historical evidence.
            # Do not invalidate a newer state merely because its source arrived earlier.
            later = [old for old in matches if old['status'] != 'forgotten' and old['valid_from'] > start
                     and old['fact_id'] != correction_target]
            successor = min(later, key=lambda old: old['valid_from']) if later else None
            if successor:
                self.invalidate(db, fact_id, successor['fact_id'], successor['valid_from'],
                                 'Historical evidence precedes a later known state', candidate_id)
            for old in matches:
                if old['status'] == 'forgotten' or old['fact_id'] == fact_id:
                    continue
                if old['valid_from'] <= start and (old['valid_to'] is None or old['valid_to'] > start):
                    self.invalidate(db, old['fact_id'], fact_id, start,
                                     'Superseded by explicit statement of an exclusive attribute', candidate_id)
        return fact_id

    def invalidate(self, db: Connection, old_id, new_id, end, reason, candidate_id=None, status='invalidated'):
        db.execute('''UPDATE memory_facts SET status=?, valid_to=?, invalidated_by=?,
            invalidation_reason=?, updated_at=? WHERE fact_id=?''',
            (status, end, new_id, reason, timestamp(), old_id))
        self.record_decision(db, candidate_id, old_id, new_id, status, reason)

    def _operation_source(self, source, operation_id):
        source = dict(source)
        if not isinstance(source.get('quote'), str) or not source['quote'].strip():
            raise ValueError('Explicit operation requires the original user statement')
        if not source.get('recorded_at'):
            raise ValueError('Explicit operation requires original receipt time')
        source['recorded_at'] = timestamp(source['recorded_at'])
        source['occurred_at'] = timestamp(source['occurred_at']) if source.get('occurred_at') else None
        source.setdefault('source_task_id', 'user-operation')
        source.setdefault('source_event_id', operation_id or uuid.uuid4().hex)
        source.setdefault('trajectory_path', '')
        return source

    def operate(self, db: Connection, action, fact, source, operation_id, target=None):
        payload = json.dumps([action, fact, source, target], ensure_ascii=False, sort_keys=True)
        value = self.validate(fact) if fact is not None else None
        evidence = self._operation_source(source, operation_id)
        if operation_id:
            prior = db.execute('SELECT * FROM memory_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if prior:
                if prior['payload'] != payload:
                    raise ValueError('Operation id already used for different input')
                return prior['result']
        old = db.execute('SELECT * FROM memory_facts WHERE fact_id=?', (target,)).fetchone() if target else None
        if target and old is None:
            raise ValueError('Unknown memory fact')
        if action == 'forget':
            result = target
            if old['status'] != 'forgotten':
                self.invalidate(db, target, None, old['valid_to'] or max(old['valid_from'], evidence['recorded_at']),
                                 'User explicitly requested forgetting', status='forgotten')
            db.execute('INSERT OR IGNORE INTO fact_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (target, evidence['source_task_id'], evidence['source_event_id'], evidence['trajectory_path'],
                        evidence['quote'], evidence['recorded_at'], evidence['occurred_at'], 'user_forget'))
        else:
            if action == 'correct' and (value['subject'], value['predicate']) != (old['subject'], old['predicate']):
                raise ValueError('A correction must identify the same subject and predicate')
            result = self.write_fact(db, value, [evidence],
                                      'user_correction' if action == 'correct' else 'user_remember',
                                      correction_target=target if action == 'correct' else None)
            if action == 'correct' and target != result:
                end = value['occurred_at'] or evidence['recorded_at']
                self.invalidate(db, target, result, max(old['valid_from'], end), 'Explicit user correction')
        if operation_id:
            db.execute('INSERT INTO memory_operations VALUES (?, ?, ?, ?)',
                       (operation_id, payload, result, timestamp()))
        return result
