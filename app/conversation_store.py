from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from types import TracebackType


@dataclass(frozen=True)
class ConversationTurn:
    user_text: str
    assistant_text: str


class _ChatLockHandle:
    def __init__(self, lock: Lock) -> None:
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire()
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._lock.release()
        return False


class ConversationStore:
    def __init__(self, *, max_rounds: int) -> None:
        if max_rounds <= 0:
            raise ValueError("max_rounds must be greater than zero")
        self._max_rounds = max_rounds
        self._history: dict[int, list[ConversationTurn]] = defaultdict(list)
        self._locks: dict[int, Lock] = defaultdict(Lock)
        self._state_lock = Lock()

    def lock_chat(self, chat_id: int) -> _ChatLockHandle:
        with self._state_lock:
            lock = self._locks[chat_id]
        return _ChatLockHandle(lock)

    def get_history(self, chat_id: int) -> list[ConversationTurn]:
        with self._state_lock:
            return list(self._history.get(chat_id, ()))

    def append_turn(self, chat_id: int, turn: ConversationTurn) -> None:
        with self._state_lock:
            history = self._history[chat_id]
            history.append(turn)
            if len(history) > self._max_rounds:
                del history[:-self._max_rounds]
