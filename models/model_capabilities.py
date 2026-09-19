"""Endpoint-scoped model capacity data; unknown models never inherit a default."""

import json
from pathlib import Path
from urllib.parse import urlsplit


def endpoint_identity(base_url: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    path = parts.path
    for suffix in ("/chat/completions", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def positive_tokens(value):
    return value if type(value) is int and value > 0 else None


def load_capability(base_url: str, model: str, path: Path | None = None) -> dict:
    source_path = path or Path(__file__).with_name("model_capabilities.json")
    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
        entries = data["models"]
        if not isinstance(entries, list):
            raise ValueError("models 必须是数组")
        matches = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("base_url"), str):
                raise ValueError("每项必须包含 base_url")
            ids = entry.get("model_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
                raise ValueError("model_ids 必须是非空字符串数组")
            if not positive_tokens(entry.get("context_window_tokens")):
                raise ValueError("context_window_tokens 必须是正整数")
            for key in ("max_output_tokens", "max_input_tokens"):
                if key in entry and not positive_tokens(entry[key]):
                    raise ValueError(f"{key} 必须是正整数")
            if not entry.get("source") or not entry.get("checked_at"):
                raise ValueError("必须记录 source 和 checked_at")
            if endpoint_identity(entry["base_url"]) == endpoint_identity(base_url) and model in ids:
                matches.append(entry)
        if len(matches) > 1:
            raise ValueError("同一接口和模型存在重复能力配置")
        return matches[0] if matches else {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"模型能力配置无效 ({source_path}): {exc}") from exc
