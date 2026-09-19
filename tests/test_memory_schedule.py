from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path

from memory.memory_service import MemoryService


def test_nightly_batch_rolls_over_at_local_three_am():
    with TemporaryDirectory() as directory:
        memory = MemoryService(Path(directory))
        try:
            # Catch up the preceding night, then run the newly due local night.
            assert memory.consolidate_due(None, now=datetime.fromisoformat('2026-09-16T02:59:00+08:00'))
            assert memory.consolidate_due(None, now=datetime.fromisoformat('2026-09-16T03:00:00+08:00'))
            assert not memory.consolidate_due(None, now=datetime.fromisoformat('2026-09-16T03:01:00+08:00'))
        finally:
            memory.close()
