from __future__ import annotations

from typing import Protocol

from openai import OpenAI

from jarvis.config import LlmConfig


class EmbeddingProvider(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, config: LlmConfig, *, client: OpenAI | None = None) -> None:
        self._client = client or OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=float(config.timeout_seconds),
        )
        self._model = config.embedding_model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        clean_texts = [text for text in texts if text.strip()]
        if not clean_texts:
            return [[] for _ in texts]
        response = self._client.embeddings.create(
            model=self._model,
            input=clean_texts,
        )
        vectors = [list(item.embedding) for item in response.data]
        embedded_by_index = iter(vectors)
        resolved: list[list[float]] = []
        for text in texts:
            if text.strip():
                resolved.append(next(embedded_by_index))
            else:
                resolved.append([])
        return resolved
