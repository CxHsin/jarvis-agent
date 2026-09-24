"""Application-owned model clients and session lifetimes."""

from dataclasses import replace
from contextlib import ExitStack

from configuration import Config
from models.model_client import ChatCompletionsClient


class Application:
    def __init__(self, config: Config, *, client=None, compression_client=None):
        self.config = config
        self.client = client if client is not None else ChatCompletionsClient(config)
        self.compression_client = compression_client
        self._sessions = set()
        self._ready = False
        self._closed = False

    def prepare(self):
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
        self._ready = True

    def create_session(self, *, resume=None, store=None, **tool_host):
        from agent.agent import Agent
        if self._closed:
            raise RuntimeError('Application is closed')
        return Agent(self.config, resume=resume, store=store, application=self, **tool_host)

    def session_started(self, session):
        self._sessions.add(session)

    def session_closed(self, session):
        self._sessions.discard(session)

    def close(self):
        if self._closed:
            return
        self._closed = True
        with ExitStack() as cleanup:
            closed = set()
            for client in (self.client, self.compression_client):
                if client is not None and id(client) not in closed:
                    closed.add(id(client))
                    close = getattr(client, 'close', None)
                    if close is not None:
                        cleanup.callback(close)
            for session in tuple(self._sessions):
                cleanup.callback(session.close)

    def __enter__(self):
        if self._closed:
            raise RuntimeError('Application is closed')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
