from app.conversation_store import ConversationStore
from app.memory_store import ConsolidationState, MemorySnapshot
from app.turns.context import PassiveTurnCommitResult, PassiveTurnContext
from app.turns.orchestrator import PassiveTurnOrchestrator

from tests.test_agent import StubLLMClient, StubMemoryStore


class _RecordingStage:
    def __init__(self, *, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def run(self, context: PassiveTurnContext):  # noqa: ANN201
        self._calls.append(self._name)
        return context


class _CommitRecordingStage:
    def __init__(self, *, calls: list[str]) -> None:
        self._calls = calls

    def run(self, context: PassiveTurnContext) -> PassiveTurnCommitResult:
        self._calls.append("commit")
        return PassiveTurnCommitResult(
            reply_text="ok",
            memory_write_plan=context.memory_write_plan,  # type: ignore[arg-type]
            turn_notes=(),
            plugin_outcomes=(),
        )


def test_passive_turn_orchestrator_runs_stages_in_fixed_order() -> None:
    calls: list[str] = []
    orchestrator = PassiveTurnOrchestrator(
        llm_client=StubLLMClient(replies=["unused"]),  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=ConversationStore(max_rounds=3),
        memory_store=StubMemoryStore(),  # type: ignore[arg-type]
    )
    orchestrator._load_context = _RecordingStage(name="load_context", calls=calls)  # type: ignore[attr-defined]
    orchestrator._build_prompt = _RecordingStage(name="build_prompt", calls=calls)  # type: ignore[attr-defined]
    orchestrator._run_reasoning = _RecordingStage(name="run_reasoning", calls=calls)  # type: ignore[attr-defined]
    orchestrator._post_reply = _RecordingStage(name="post_reply", calls=calls)  # type: ignore[attr-defined]
    orchestrator._commit = _CommitRecordingStage(calls=calls)  # type: ignore[attr-defined]

    result = orchestrator.run(chat_id=1, user_text="hello")

    assert result.reply_text == "ok"
    assert calls == [
        "load_context",
        "build_prompt",
        "run_reasoning",
        "post_reply",
        "commit",
    ]
