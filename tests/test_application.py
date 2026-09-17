from copy import deepcopy
from threading import Event
import pytest

from jarvis_agent import Config
from tests.test_memory_recall_contract import remember


class Client:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools, tool_choice='auto'):
        self.requests.append(deepcopy(messages))
        return {'role': 'assistant', 'content': 'done'}


def test_sessions_share_memory_without_sharing_messages_or_lifetime(tmp_path):
    from application import Application

    extracted = Event()

    class Extraction:
        def complete(self, messages, tools, tool_choice='auto'):
            extracted.set()
            return {'content': '{"candidates": []}'}

    client = Client()
    config = Config('http://example.test', '', 'test', root_dir=tmp_path,
                    state_dir=tmp_path / 'state', recent_task_count=1)
    with Application(config, client=client, extraction_client=Extraction()) as app:
        first = app.create_session()
        second = app.create_session()
        remember(first.store.memory, 'tea')
        assert second.store.memory.search('tea')['facts'][0]['object'] == 'tea'
        first.run_request('first private conversation')
        second.run_request('second private conversation')
        assert 'first private conversation' not in str(client.requests[-1])
        first.close()
        remember(second.store.memory, 'coffee')
        assert second.store.memory.search('coffee')['facts'][0]['object'] == 'coffee'
        second.run_request('still working')
        assert 'still working' in str(client.requests[-1])
        assert extracted.wait(2), 'closing another session must leave shared extraction available'


def test_application_preserves_session_locks_and_releases_them_on_exit(tmp_path):
    from application import Application
    from session_store import SessionLockedError

    config = Config('http://example.test', '', 'test', root_dir=tmp_path,
                    state_dir=tmp_path / 'state')
    with Application(config, client=Client(), extraction_client=Client()) as app:
        first = app.create_session()
        session_id = first.store.session_id
        first.run_request('persisted conversation')
        with pytest.raises(SessionLockedError):
            app.create_session(resume=session_id)
        first.run_request('lock failure does not stop this session')
    with pytest.raises(RuntimeError, match='closed'):
        app.create_session()
    with Application(config, client=Client(), extraction_client=Client()) as restored:
        session = restored.create_session(resume=session_id)
        assert 'persisted conversation' in str(session.messages)


def test_application_closes_shared_injected_client_once(tmp_path):
    from application import Application

    class CloseableClient(Client):
        closed = 0

        def close(self):
            self.closed += 1

    client = CloseableClient()
    config = Config('http://example.test', '', 'test', root_dir=tmp_path,
                    state_dir=tmp_path / 'state')
    app = Application(config, client=client, compression_client=client,
                      extraction_client=client, rewrite_client=client,
                      memory_authorization_client=client)
    first, second = app.create_session(), app.create_session()
    first.close()
    assert client.closed == 0
    second.run_request('still available')
    app.close()
    app.close()
    assert client.closed == 1
