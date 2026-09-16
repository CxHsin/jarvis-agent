"""Versioned, bounded USER profile projections and explicit structured edits."""

import json
import math
import uuid
from contextlib import contextmanager

from stable_memory import timestamp


PROFILE_CATEGORIES = ('identity', 'work_preferences', 'communication',
                      'long_term_goals', 'constraints', 'current_state')
PROFILE_HEADER = '# User profile\n\n'
DEFAULT_SELF = '''# Jarvis

## Role
You are Jarvis, a general personal assistant for one local user.

## Principles
Use evidence, distinguish uncertainty, and respect the user's stated preferences.

## Capabilities
Work within the capabilities of the currently available tools. Do not claim
access, actions, or results that have not been verified.

## Tool constraints
Follow the tool schemas and workspace constraints supplied by the runtime.
Ask the user when their intent is unclear.
'''


class ProfileEditError(ValueError):
    """An unaccepted human edit remains on disk for correction."""


def profile_tokens(content):
    return math.ceil(len(content.encode('utf-8')) / 4)


class ProfileMemory:
    def _init_profile(self, db):
        self.profile_path = self.directory / 'memory.md'
        self.self_path = self.directory / 'self.md'
        db.execute('''CREATE TABLE IF NOT EXISTS profile_versions (
            version INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
            source_fact_ids TEXT NOT NULL, token_count INTEGER NOT NULL,
            created_at TEXT NOT NULL, activated_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS profile_publication (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            observed TEXT NOT NULL, content TEXT NOT NULL)''')
        if not db.execute('SELECT 1 FROM profile_versions').fetchone():
            now = timestamp()
            db.execute('INSERT INTO profile_versions(content,source_fact_ids,token_count,created_at,activated_at) VALUES (?,?,?,?,?)',
                       (PROFILE_HEADER, '[]', profile_tokens(PROFILE_HEADER), now, now))
        if not self.profile_path.exists():
            self.profile_path.write_text(self._latest_profile(db)['content'], encoding='utf-8')
        # A row seen during startup came from a previously committed transaction.
        self._recover_profile_publication(db)
        try:
            with self.self_path.open('x', encoding='utf-8') as stream:
                stream.write(DEFAULT_SELF)
        except FileExistsError:
            pass

    def _latest_profile(self, db):
        row = dict(db.execute('SELECT * FROM profile_versions ORDER BY version DESC LIMIT 1').fetchone())
        row['source_fact_ids'] = json.loads(row['source_fact_ids'])
        return row

    def _recover_profile_publication(self, db):
        pending = db.execute('SELECT * FROM profile_publication WHERE singleton=1').fetchone()
        if pending is None:
            return True
        observed = self.profile_path.read_text(encoding='utf-8')
        if observed not in (pending['observed'], pending['content']):
            # A newer human edit wins over a delayed projection. Never overwrite it.
            return False
        if observed != pending['content']:
            temporary = self.profile_path.with_name(f'.memory-{uuid.uuid4().hex}.tmp')
            try:
                temporary.write_text(pending['content'], encoding='utf-8')
                if self.profile_path.read_text(encoding='utf-8') != observed:
                    return False
                temporary.replace(self.profile_path)
            finally:
                temporary.unlink(missing_ok=True)
        db.execute('DELETE FROM profile_publication WHERE singleton=1')
        return True

    @contextmanager
    def _profile_transaction(self):
        """Commit fact IDs and the publication intent before exposing either on disk."""
        with self._lock:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not self._recover_profile_publication(db):
                    raise ProfileEditError('memory.md changed after an interrupted publication; preserve your edit and restore the committed profile before retrying')
                yield db
            # A crash here leaves the durable outbox for the next task or startup.
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not self._recover_profile_publication(db):
                    raise ProfileEditError('memory.md changed after profile commit; your newer edit was preserved')

    def profile_snapshot(self):
        """Read the activated DB projection; never use Markdown for recall."""
        with self._lock, self._connect() as db:
            return self._latest_profile(db)

    def task_prefix(self):
        """Take one immutable prefix snapshot at the start of a task."""
        with self._lock:
            self.import_profile_edits()
            return self.prefix_snapshot()

    def prefix_snapshot(self):
        """Read the last accepted profile with the user-maintained Agent self."""
        with self._lock:
            return self.self_path.read_text(encoding='utf-8') + '\n\n' + self.profile_snapshot()['content']

    def _parse_profile(self, content):
        if not content.startswith(PROFILE_HEADER):
            raise ProfileEditError('memory.md must start with # User profile and a blank line')
        category = None
        rows = []
        seen = set()
        for line in content[len(PROFILE_HEADER):].splitlines():
            if not line.strip():
                continue
            if line.startswith('## ') and line[3:] in PROFILE_CATEGORIES:
                category = line[3:]
                continue
            try:
                if not line.startswith('- ') or category is None:
                    raise ValueError()
                row = json.loads(line[2:])
                if not isinstance(row, dict) or set(row) != {'fact_id', 'predicate', 'object'}:
                    raise ValueError()
                if row['fact_id'] is not None:
                    if not isinstance(row['fact_id'], str) or row['fact_id'] in seen:
                        raise ValueError()
                    seen.add(row['fact_id'])
                row = self._fact_values(dict(row, subject='USER', category=category, confidence=1.0))
            except (ValueError, TypeError) as exc:
                raise ProfileEditError('Invalid memory.md row: use JSON fact_id, predicate, object under a fixed category; new fact_id is null') from exc
            rows.append(row)
        return rows

    def import_profile_edits(self):
        """Validate the entire edit before atomically applying user corrections."""
        with self._profile_transaction() as db:
            previous = self._latest_profile(db)
            observed = self.profile_path.read_text(encoding='utf-8')
            if observed == previous['content']:
                return False
            rows = self._parse_profile(observed)
            original = {row['fact_id']: row for row in self._parse_profile(previous['content'])}
            for row in rows:
                if row['fact_id'] is not None:
                    old = original.get(row['fact_id'])
                    if old is None or (row['predicate'], row['category']) != (old['predicate'], old['category']):
                        raise ProfileEditError('Keep existing fact_id, predicate and category unchanged; edit object, remove the row, or add a row with fact_id null')
            now = timestamp()
            edit_id = uuid.uuid4().hex
            source = dict(source_task_id='profile-edit', source_event_id=edit_id,
                          trajectory_path=str(self.profile_path), quote=observed, recorded_at=now, occurred_at=None)
            remaining = set()
            for row in rows:
                target = row['fact_id']
                remaining.add(target)
                if target is not None and row['object'] == original[target]['object']:
                    continue
                result = self._write_fact(db, row, [source], 'user_correction', correction_target=target)
                if target and result != target:
                    self._invalidate(db, target, result, now, 'Explicit memory.md user correction')
            for target in original.keys() - remaining:
                self._invalidate(db, target, None, now, 'User removed memory.md fact', status='forgotten')
                db.execute('INSERT INTO fact_sources VALUES (?,?,?,?,?,?,?,?)',
                           (target, source['source_task_id'], edit_id, str(self.profile_path), observed, now, None, 'user_forget'))
            self._publish_profile(db, previous, observed)
            return True

    def profile_versions(self):
        with self._lock, self._connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM profile_versions ORDER BY version')]
        for row in rows:
            row['source_fact_ids'] = json.loads(row['source_fact_ids'])
        return rows

    def consolidate(self, client):
        success = super().consolidate(client)
        if success:
            self.refresh_profile()
        return success

    def correct(self, fact_id, fact, *, source, operation_id=None):
        result = super().correct(fact_id, fact, source=source, operation_id=operation_id)
        self._refresh_after_operation()
        return result

    def forget(self, fact_id, *, source, operation_id=None):
        result = super().forget(fact_id, source=source, operation_id=operation_id)
        self._refresh_after_operation()
        return result

    def _refresh_after_operation(self):
        try:
            self.refresh_profile()
        except ProfileEditError:
            # The user will resolve/import the file at the next task boundary.
            pass

    def refresh_profile(self):
        """Publish active facts at a consolidation boundary without version churn."""
        with self._profile_transaction() as db:
            previous = self._latest_profile(db)
            observed = self.profile_path.read_text(encoding='utf-8')
            if observed != previous['content']:
                raise ProfileEditError('memory.md contains user edits; import them at the next task before consolidation')
            return self._publish_profile(db, previous, observed)

    def _publish_profile(self, db, previous, observed):
        now = timestamp()
        facts = list(db.execute("SELECT * FROM memory_facts WHERE status!='forgotten' AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY importance DESC, fact_id", (now, now)))
        content = PROFILE_HEADER
        ids = []
        for category in PROFILE_CATEGORIES:
            section = ''
            for fact in facts:
                if fact['category'] != category:
                    continue
                row = {key: fact[key] for key in ('fact_id', 'predicate', 'object')}
                line = '- ' + json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
                addition = ('' if section else f'## {category}\n\n') + line
                if profile_tokens(content + section + addition + '\n') <= 1500:
                    section += addition
                    ids.append(fact['fact_id'])
            if section:
                content += section + '\n'
        if self.profile_path.read_text(encoding='utf-8') != observed:
            raise ProfileEditError('memory.md changed while preparing the profile; retry the task')
        if content != previous['content']:
            db.execute('INSERT INTO profile_versions(content,source_fact_ids,token_count,created_at,activated_at) VALUES (?,?,?,?,?)',
                       (content, json.dumps(ids), profile_tokens(content), now, now))
        if observed != content:
            db.execute('INSERT OR REPLACE INTO profile_publication(singleton,observed,content) VALUES (1,?,?)',
                       (observed, content))
        return self._latest_profile(db)
