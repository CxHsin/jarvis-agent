"""Application-owned clients, personal memory and session lifetimes."""

from dataclasses import replace
from contextlib import ExitStack

from configuration import Config
from memory_service import MemoryService, OpenAIEmbeddingClient
from model_client import ChatCompletionsClient
from session_store import resolve_state_dir


def assemble_memory(config, *, embedding_client=None, rewrite_client=None):
    """Also supports standalone SessionStore compatibility without a worker."""
    from session_migration import protect_legacy_memory
    protect_legacy_memory(config)
    endpoint = getattr(config, 'embedding_base_url', None)
    model = getattr(config, 'embedding_model', None)
    if embedding_client is None and endpoint and model:
        embedding_client = OpenAIEmbeddingClient(endpoint, getattr(config, 'embedding_api_key', ''),
                                                 model, getattr(config, 'request_timeout', 60.0))
    return MemoryService(resolve_state_dir(config) / 'memory', embedding_client=embedding_client,
                         rewrite_client=rewrite_client, embedding_model=model,
                         embedding_dimensions=getattr(config, 'embedding_dimensions', 1536))


def standalone_store(config, *, session_id=None, resume=False):
    """Legacy storage entry points get assembled memory, without starting a worker.

    Hosts that start background work must own and close that memory explicitly,
    or use Application for automatic lifetime management.
    """
    from session_store import SessionStore
    memory = assemble_memory(config)
    if resume:
        return SessionStore.resume(config, session_id, memory=memory)
    return SessionStore.create(config, memory=memory)


class Application:
    def __init__(self, config: Config, *, client=None, compression_client=None,
                 extraction_client=None, embedding_client=None, rewrite_client=None,
                 memory_authorization_client=None):
        self.config = config
        self.client = client if client is not None else ChatCompletionsClient(config)
        self.compression_client = compression_client
        self.extraction_client = extraction_client
        self.embedding_client = embedding_client
        self.rewrite_client = rewrite_client
        self.memory_authorization_client = memory_authorization_client
        self.memory = None
        self._sessions = set()
        self._ready = False
        self._closed = False

    def prepare(self, memory=None):
        if self._closed:
            raise RuntimeError('Application is closed')
        if self._ready:
            return
        config = self.config
        if isinstance(self.client, ChatCompletionsClient):
            self.config = self.client.resolve_config()
            print(f"[模型容量] {config.model}: 窗口={self.config.context_window_tokens} "
                  f"输出上限={self.config.max_output_tokens} 窗口来源={self.config.context_window_source} "
                  f"能力来源={self.config.capability_source} 核对日期={self.config.capability_checked_at}")
        if self.compression_client is not None:
            if isinstance(self.compression_client, ChatCompletionsClient):
                self.compression_client.resolve_config()
        elif isinstance(self.client, ChatCompletionsClient) and (
            config.compression_model and config.compression_model != config.model
            or config.compression_max_output_tokens is not None
            or config.compression_context_window_tokens is not None
        ):
            compression_config = replace(config, model=config.compression_model or config.model,
                context_window_tokens=config.compression_context_window_tokens,
                context_window_source='configured' if config.compression_context_window_tokens else 'unknown',
                max_output_tokens=config.compression_max_output_tokens or config.max_output_tokens,
                context_keep_recent_tokens=1)
            self.compression_client = ChatCompletionsClient(compression_config)
            self.compression_client.resolve_config()
        else:
            self.compression_client = self.client
        self.memory_authorization_client = self.memory_authorization_client or ChatCompletionsClient(self.config)
        self.extraction_client = self.extraction_client or ChatCompletionsClient(self.config)
        self.memory = memory or assemble_memory(self.config, embedding_client=self.embedding_client,
                                                rewrite_client=self.rewrite_client or self.client)
        if memory is not None:
            self.memory.configure_retrieval(
                embedding_client=self.embedding_client if self.embedding_client is not None else memory.embedding_client,
                rewrite_client=self.rewrite_client or self.client, embedding_model=self.config.embedding_model)
        self._ready = True

    def create_session(self, *, resume=None, store=None, **tool_host):
        from jarvis_agent import Agent
        if self._closed:
            raise RuntimeError('Application is closed')
        return Agent(self.config, resume=resume, store=store, application=self, **tool_host)

    def session_started(self, session):
        self._sessions.add(session)
        self.memory.start_worker(self.extraction_client)

    def session_closed(self, session):
        self._sessions.discard(session)

    def close(self):
        if self._closed:
            return
        self._closed = True
        clients = (self.client, self.compression_client, self.extraction_client,
                   self.embedding_client, self.rewrite_client, self.memory_authorization_client)
        # All cleanup runs even if a host-provided client fails to close.
        with ExitStack() as cleanup:
            closed = set()
            for client in clients:
                if client is not None and id(client) not in closed:
                    closed.add(id(client))
                    close = getattr(client, 'close', None)
                    if close is not None:
                        cleanup.callback(close)
            if self.memory is not None:
                cleanup.callback(self.memory.close, wait=True)
            for session in tuple(self._sessions):
                cleanup.callback(session.close)

    def __enter__(self):
        if self._closed:
            raise RuntimeError('Application is closed')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
