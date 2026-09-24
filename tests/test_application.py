from copy import deepcopy
import pytest

from application import Application
from configuration import Config


class Client:
    def __init__(self):
        self.requests = []
        self.closed = 0

    def complete(self, messages, tools, tool_choice='auto'):
        self.requests.append(deepcopy(messages))
        return {'role': 'assistant', 'content': 'done'}

    def close(self):
        self.closed += 1


def config(tmp_path):
    return Config('http://example.test', '', 'test', root_dir=tmp_path,
                  state_dir=tmp_path / 'state', recent_task_count=1)


def test_sessions_share_client_not_context_or_memory(tmp_path):
    client = Client()
    with Application(config(tmp_path), client=client) as app:
        first, second = app.create_session(), app.create_session()
        first.run_request('first private conversation')
        second.run_request('second private conversation')
        assert 'first private conversation' not in str(client.requests[-1])
        assert not (tmp_path / 'state' / 'memory').exists()
        first.close()
        second.run_request('still working')
        assert 'still working' in str(client.requests[-1])
    assert client.closed == 1


def test_application_preserves_session_locks_and_releases_them_on_exit(tmp_path):
    from session.session_store import SessionLockedError
    with Application(config(tmp_path), client=Client()) as app:
        first = app.create_session()
        session_id = first.store.session_id
        first.run_request('persisted conversation')
        with pytest.raises(SessionLockedError):
            app.create_session(resume=session_id)
        first.run_request('lock failure does not stop this session')
    with pytest.raises(RuntimeError, match='closed'):
        app.create_session()
    with Application(config(tmp_path), client=Client()) as restored:
        session = restored.create_session(resume=session_id)
        assert 'persisted conversation' in str(session.messages)


def test_application_closes_shared_injected_client_once(tmp_path):
    client = Client()
    app = Application(config(tmp_path), client=client, compression_client=client)
    first, second = app.create_session(), app.create_session()
    first.close()
    assert client.closed == 0
    second.run_request('still available')
    app.close()
    app.close()
    assert client.closed == 1
