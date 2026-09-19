"""Per-call server usage, with unknown values kept distinct from zero."""

from copy import deepcopy
from typing import Any, Mapping


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def cache_usage(usage: Any) -> dict:
    raw = usage if isinstance(usage, Mapping) else {}
    total = token_count(raw.get("prompt_tokens"))
    hit = token_count(raw.get("prompt_cache_hit_tokens"))
    miss = token_count(raw.get("prompt_cache_miss_tokens"))
    details = raw.get("prompt_tokens_details")
    nested = token_count(details.get("cached_tokens")) if isinstance(details, Mapping) else None
    if hit is None:
        hit = nested
    inconsistent = nested is not None and hit is not None and nested != hit
    if total is None and hit is not None and miss is not None:
        total = hit + miss
    if total is not None:
        inconsistent |= hit is not None and hit > total
        inconsistent |= miss is not None and miss > total
        inconsistent |= hit is not None and miss is not None and hit + miss != total
    if inconsistent:
        hit = miss = None
    return {"input": total, "hit": hit, "miss": miss,
            "ratio": hit / total if hit is not None and total else None}


def display(value):
    return "未知" if value is None else str(value)


class UsageLedger:
    def __init__(self):
        self.records: list[dict] = []

    def reset(self):
        self.records.clear()

    def record(self, role, model, raw, failed=False):
        metrics = cache_usage(raw)
        self.records.append({"role": role, "model": model, "raw_usage": deepcopy(raw),
                             "failed": failed, **metrics})
        ratio = "未知" if metrics["ratio"] is None else f"{metrics['ratio']:.1%}"
        print(f"[缓存/{role}] model={model} 输入={display(metrics['input'])} "
              f"命中={display(metrics['hit'])} 占比={ratio}" + ("（调用失败）" if failed else ""))

    def summary(self):
        for role in ("主模型", "压缩模型"):
            rows = [row for row in self.records if row["role"] == role]
            if not rows:
                continue
            total = sum(row["input"] for row in rows) if all(row["input"] is not None for row in rows) else None
            hit = sum(row["hit"] for row in rows) if all(row["hit"] is not None for row in rows) else None
            ratio = f"{hit / total:.1%}" if hit is not None and total else "未知"
            known = sum(row["input"] is not None and row["hit"] is not None for row in rows)
            print(f"[缓存汇总/{role}] 调用={len(rows)} 指标完整={known}/{len(rows)} "
                  f"输入={display(total)} 命中={display(hit)} 占比={ratio}")


class MeasuredClient:
    def __init__(self, client, ledger, role, model):
        self.client, self.ledger, self.role, self.model = client, ledger, role, model
        self.last_usage = None

    def complete(self, messages, tools, tool_choice="auto"):
        self.last_usage = None
        if hasattr(self.client, "last_usage"):
            self.client.last_usage = None
        failed = True
        try:
            result = self.client.complete(messages, tools, tool_choice)
            failed = False
            return result
        finally:
            self.last_usage = deepcopy(getattr(self.client, "last_usage", None))
            self.ledger.record(self.role, self.model, self.last_usage, failed)
