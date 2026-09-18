"""Observe background memory completion through its public status contract."""

from threading import Event
from time import monotonic


def wait_state(memory, state, timeout=3):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if memory.vector_status()['state'] == state:
            return
        Event().wait(0.01)
    raise AssertionError(memory.vector_status())


def wait_ready(memory):
    wait_state(memory, 'ready')
