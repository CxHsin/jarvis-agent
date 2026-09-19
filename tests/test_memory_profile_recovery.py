"""Profile persistence failures must not turn generated IDs into manual edits."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from memory.memory_service import MemoryService
from memory.memory_profile import ProfileEditError


def user_edit(name='Alice'):
    return '# User profile\n\n## identity\n\n- ' + json.dumps(
        dict(fact_id=None, predicate='preferred_name', object=name)) + '\n'


def test_failed_import_commit_preserves_original_edit_and_can_retry(tmp_path):
    memory = MemoryService(tmp_path)
    edited = user_edit()
    memory.profile_path.write_text(edited, encoding='utf-8')
    connect = sqlite3.connect

    class FailedCommit(sqlite3.Connection):
        dirty_profile = False

        def execute(self, sql, *args, **kwargs):
            if sql.startswith('INSERT INTO profile_versions'):
                self.dirty_profile = True
            return super().execute(sql, *args, **kwargs)

        def __exit__(self, exc_type, exc, traceback):
            if self.dirty_profile and exc_type is None:
                self.rollback()
                raise sqlite3.OperationalError('simulated commit failure')
            return super().__exit__(exc_type, exc, traceback)

    with patch('sqlite3.connect', side_effect=lambda *a, **kw: connect(*a, factory=FailedCommit, **kw)):
        with pytest.raises(sqlite3.OperationalError, match='commit failure'):
            memory.import_profile_edits()
    assert memory.profile_path.read_text(encoding='utf-8') == edited
    assert memory.facts() == []
    restarted = MemoryService(tmp_path)
    assert 'Alice' in restarted.task_prefix()
    assert len(restarted.facts()) == 1


def test_committed_import_recovers_after_file_publication_failure(tmp_path):
    memory = MemoryService(tmp_path)
    edited = user_edit()
    memory.profile_path.write_text(edited, encoding='utf-8')
    with patch.object(Path, 'replace', side_effect=OSError('simulated interrupted publication')):
        with pytest.raises(OSError, match='interrupted publication'):
            memory.import_profile_edits()
    assert memory.profile_path.read_text(encoding='utf-8') == edited
    assert len(memory.facts()) == 1
    committed = memory.profile_snapshot()
    restarted = MemoryService(tmp_path)
    assert restarted.profile_path.read_text(encoding='utf-8') == committed['content']
    assert restarted.import_profile_edits() is False
    assert restarted.profile_snapshot() == committed
    assert len(restarted.facts()) == 1


def test_recovery_preserves_newer_manual_edit_until_user_resolves_it(tmp_path):
    memory = MemoryService(tmp_path)
    memory.profile_path.write_text(user_edit(), encoding='utf-8')
    with patch.object(Path, 'replace', side_effect=OSError('interrupted publication')):
        with pytest.raises(OSError):
            memory.import_profile_edits()
    newer = user_edit('Bob')
    memory.profile_path.write_text(newer, encoding='utf-8')
    restarted = MemoryService(tmp_path)
    assert restarted.profile_path.read_text(encoding='utf-8') == newer
    with pytest.raises(ProfileEditError, match='interrupted publication'):
        restarted.task_prefix()
    assert restarted.profile_path.read_text(encoding='utf-8') == newer
    assert restarted.facts()[0]['object'] == 'Alice'
    # Resolve against the committed IDs, then apply the user's newer correction.
    committed = restarted.profile_snapshot()['content']
    restarted.profile_path.write_text(committed, encoding='utf-8')
    restarted.task_prefix()
    restarted.profile_path.write_text(committed.replace('Alice', 'Bob'), encoding='utf-8')
    assert 'Bob' in restarted.task_prefix()
    assert restarted.facts()[0]['object'] == 'Bob'


def test_restart_after_file_replace_before_publication_acknowledgement(tmp_path):
    memory = MemoryService(tmp_path)
    memory.profile_path.write_text(user_edit(), encoding='utf-8')
    connect = sqlite3.connect

    class FailedAcknowledgement(sqlite3.Connection):
        publication_acknowledged = False

        def execute(self, sql, *args, **kwargs):
            if sql.startswith('DELETE FROM profile_publication'):
                self.publication_acknowledged = True
            return super().execute(sql, *args, **kwargs)

        def __exit__(self, exc_type, exc, traceback):
            if self.publication_acknowledged and exc_type is None:
                self.rollback()
                raise sqlite3.OperationalError('publication acknowledgement failed')
            return super().__exit__(exc_type, exc, traceback)

    with patch('sqlite3.connect', side_effect=lambda *a, **kw: connect(*a, factory=FailedAcknowledgement, **kw)):
        with pytest.raises(sqlite3.OperationalError, match='acknowledgement failed'):
            memory.import_profile_edits()
    committed = memory.profile_snapshot()
    restarted = MemoryService(tmp_path)
    assert restarted.profile_path.read_text(encoding='utf-8') == committed['content']
    assert restarted.import_profile_edits() is False
    assert restarted.profile_snapshot() == committed
    assert len(restarted.facts()) == 1
