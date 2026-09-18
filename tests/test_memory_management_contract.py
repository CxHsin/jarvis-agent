import json

import pytest

from jarvis_agent import Agent, Config
from tests.test_memory_recall_contract import remember


class ToolClient:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools, tool_choice):
        self.calls += 1
        if self.calls > 2:
            return {'role': 'assistant', 'content': 'done'}
        name, args = ('tool_search', {'query': 'memory'}) if self.calls == 1 else (
            'memory_manage', {'action': 'remember', 'fact': dict(subject='USER',
                predicate='health_constraint', object='avoid peanuts', category='constraints')})
        return {'role': 'assistant', 'content': None, 'tool_calls': [dict(
            id=str(self.calls), type='function', function=dict(name=name, arguments=json.dumps(args)))]}


@pytest.mark.parametrize('decision,revoked,expected', [(False, False, 0), (True, False, 1), (True, True, 0)])
def test_memory_management_checks_consent_and_revocation(tmp_path, decision, revoked, expected):
    class Authorization:
        def complete(self, messages, tools, tool_choice):
            evidence = json.loads(messages[-1]['content'])
            assert evidence['user_message'] == query
            if revoked:
                agent.tool_runtime.policy.revoke()
            return {'content': json.dumps({'authorized': decision})}

    query = 'Remember that I must avoid peanuts' if decision else 'Explain peanut allergies'
    config = Config(base_url='http://example.test', api_key='', model='test', root_dir=tmp_path,
                    state_dir=tmp_path / 'state', max_rounds=4)
    agent = Agent(config, ToolClient(), memory_authorization_client=Authorization())
    try:
        agent.run_request(query)
        assert len(agent.store.memory.facts()) == expected
        if expected:
            assert agent.store.memory.facts()[0]['sources'][0]['quote'] == query
        metadata = agent.tool_registry.get('memory_manage', '1').metadata
        assert metadata.side_effects == ('memory',)
    finally:
        agent.close()


def test_configured_embedding_survives_normal_agent_startup(tmp_path, monkeypatch):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'data': [{'embedding': [1., 0., 0.]}]}).encode()
    def urlopen(request, **kwargs):
        calls.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    config = Config(base_url='http://example.test', api_key='', model='test', root_dir=tmp_path,
                    state_dir=tmp_path / 'state', embedding_base_url='http://embedding.test/v1',
                    embedding_model='embed', embedding_dimensions=3)
    agent = Agent(config, ToolClient())
    try:
        remember(agent.store.memory, 'tea')
        result = agent.store.memory.search('tea')
        assert result['facts']
        assert result['vector_available'] is True
        assert any(call.get('model') == 'embed' for call in calls)
    finally:
        agent.close()


def test_agent_reports_failed_correction_without_partial_memory_change(tmp_path):
    import sqlite3
    from tests.test_memory_transactions import fact, source

    class Authorization:
        def complete(self, messages, tools, tool_choice):
            return {'content': '{"authorized": true}'}

    class CorrectionClient:
        calls = 0
        target = None
        results = []

        def complete(self, messages, tools, tool_choice):
            self.calls += 1
            phase = (self.calls - 1) % 3
            if phase == 2:
                self.results.append(messages[-1]['content'])
                return {'role': 'assistant', 'content': 'done'}
            name, args = ('tool_search', {'query': 'memory'}) if phase == 0 else (
                'memory_manage', {'action': 'correct', 'fact_id': self.target, 'fact': fact('English')})
            return {'role': 'assistant', 'content': None, 'tool_calls': [dict(
                id=str(self.calls), type='function', function=dict(name=name, arguments=json.dumps(args)))]}

    client = CorrectionClient()
    config = Config(base_url='http://example.test', api_key='', model='test', root_dir=tmp_path,
                    state_dir=tmp_path / 'state', max_rounds=4)
    agent = Agent(config, client, memory_authorization_client=Authorization())
    try:
        memory = agent.store.memory
        client.target = memory.remember(fact('Chinese'), source=source('Chinese'))
        memory.refresh_profile()
        before = memory.facts(True), memory.conflicts(), memory.profile_versions()
        with sqlite3.connect(memory.path) as db:
            db.execute("""CREATE TRIGGER fail_profile BEFORE INSERT ON profile_versions
                          BEGIN SELECT RAISE(ABORT, 'profile write failed'); END""")
        agent.run_request('Correct my preferred language to English')
        assert 'profile write failed' in client.results[-1]
        assert (memory.facts(True), memory.conflicts(), memory.profile_versions()) == before
        with sqlite3.connect(memory.path) as db:
            db.execute('DROP TRIGGER fail_profile')
        agent.run_request('Correct my preferred language to English')
        assert [row['object'] for row in memory.facts()] == ['English']
        assert memory.facts()[0]['sources'][0]['quote'] == 'Correct my preferred language to English'
        assert 'English' in memory.task_prefix()
    finally:
        agent.close()
