from __future__ import annotations

import json
import logging
import time

from jarvis.tools.base import ToolCallRequest, ToolCallResult, error_result, run_with_timeout
from jarvis.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        tool = self._registry.get_tool(request.tool_name)
        meta = self._registry.get_meta(request.tool_name)
        if tool is None or meta is None:
            return error_result(
                request.tool_name,
                json.dumps({"error": f"Unknown tool: {request.tool_name}"}, ensure_ascii=False),
                status="unknown",
            )

        visible = set(request.visible_tools) | set(request.unlocked_tools)
        if request.tool_name not in visible:
            return error_result(
                request.tool_name,
                json.dumps(
                    {
                        "error": f"Tool '{request.tool_name}' is not available in this turn.",
                        "hint": f"Use tool_search with select:{request.tool_name} first.",
                    },
                    ensure_ascii=False,
                ),
                status="denied",
                risk_level=meta.risk_level,
            )

        errors = tool.validate_params(request.arguments)
        if errors:
            return error_result(
                request.tool_name,
                json.dumps({"error": "Invalid arguments", "details": errors}, ensure_ascii=False),
                status="error",
                risk_level=meta.risk_level,
            )

        start = time.perf_counter()
        try:
            result = await run_with_timeout(
                tool.execute(request.arguments),
                meta.default_timeout_seconds,
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            result.duration_ms = duration_ms
            logger.info(
                "tool=%s status=%s risk=%s duration_ms=%s",
                request.tool_name,
                result.status,
                meta.risk_level,
                duration_ms,
            )
            return result
        except TimeoutError:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.warning(
                "tool=%s status=timeout risk=%s duration_ms=%s",
                request.tool_name,
                meta.risk_level,
                duration_ms,
            )
            return error_result(
                request.tool_name,
                json.dumps(
                    {"error": f"Timed out after {meta.default_timeout_seconds}s"},
                    ensure_ascii=False,
                ),
                status="timeout",
                risk_level=meta.risk_level,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.exception(
                "tool=%s status=error risk=%s duration_ms=%s",
                request.tool_name,
                meta.risk_level,
                duration_ms,
            )
            return error_result(
                request.tool_name,
                json.dumps({"error": str(exc)}, ensure_ascii=False),
                status="error",
                risk_level=meta.risk_level,
                duration_ms=duration_ms,
            )
