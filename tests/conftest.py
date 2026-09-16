"""Keep integration tests offline, including background memory workers."""

import socket

import pytest


@pytest.fixture(scope="session", autouse=True)
def offline_network():
    def unavailable(*args, **kwargs):
        raise OSError("Live network access is disabled in the test suite")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "create_connection", unavailable)
        yield
