"""Reviewed logistics capability built on the narrow, dataset-scoped session."""

from __future__ import annotations

import time
import uuid
from collections import Counter
from typing import Any

from harness.agent import RunResult, logistics_capability_request
from harness.tools import ToolError
from self_heal.settings import AnalystConfig
from self_heal.table_store import TableAccessError


TOOL_NAME = "count_customers_over_shipment_threshold"
TOOL_VERSION = "logistics-shipment-threshold-tool-v2"


class LogisticsTools:
    def __init__(self, table: Any, config: AnalystConfig) -> None:
        self.table = table
        self.config = config

    def definitions(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": TOOL_NAME,
            "description": "Count distinct customers with more than a threshold of sent shipments from a numbered warehouse yesterday in the assigned bundle.",
            "parameters": {"type": "object", "properties": {
                "warehouse_number": {"type": "integer"},
                "relative_day": {"type": "string", "enum": ["yesterday"]},
                "threshold": {"type": "integer", "minimum": 0},
            }, "required": ["warehouse_number", "relative_day", "threshold"], "additionalProperties": False},
        }}]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != TOOL_NAME or set(arguments) != {"warehouse_number", "relative_day", "threshold"}:
            raise ToolError("Logistics tool or arguments are unsupported")
        warehouse, day, threshold = arguments["warehouse_number"], arguments["relative_day"], arguments["threshold"]
        if type(warehouse) is not int or warehouse < 1 or day != "yesterday" or type(threshold) is not int or threshold < 0:
            raise ToolError("Invalid logistics task values")
        counts: Counter[str] = Counter()
        cursor: str | None = None
        started = time.monotonic()
        for _ in range(self.config.limits.max_pages):
            if time.monotonic() - started > self.config.limits.max_elapsed_seconds:
                raise ToolError("Logistics scan exceeded the time budget")
            page = self.table.read_shipments(
                warehouse_number=warehouse, relative_day=day,
                cursor=cursor, limit=self.config.limits.max_page_size,
            )
            if self.table.bytes_read > self.config.limits.max_bytes:
                raise ToolError("Logistics scan exceeded the byte budget")
            for shipment in page["shipments"]:
                counts[shipment["sender_customer_id"]] += 1
            cursor = page["next_cursor"]
            if cursor is None:
                return {"value": sum(count > threshold for count in counts.values()),
                        "shipments_scanned": sum(counts.values())}
        raise ToolError("Logistics scan exceeded the page budget")


class LogisticsAgent:
    """Execute the reviewed task through an observed model-facing tool boundary."""

    def __init__(self, model: Any, tools: Any, config: AnalystConfig) -> None:
        self.tools = tools

    def run(self, invocation: dict[str, Any] | str, *, run_id: str | None = None) -> RunResult:
        started = time.monotonic()
        request = logistics_capability_request(invocation) if isinstance(invocation, str) else None
        task = ({key: request[key] for key in ("operation", "warehouse_number", "relative_day", "threshold")}
                if request else None)
        answer: dict[str, int] | None = None
        error: str | None = None
        calls = 0
        if task is not None:
            try:
                calls = 1
                result = self.tools.execute(TOOL_NAME, {key: task[key] for key in ("warehouse_number", "relative_day", "threshold")})
                answer = {"value": result["value"]}
            except (ToolError, TableAccessError, KeyError, TypeError) as exc:
                error = str(exc)
        return RunResult(
            run_id=run_id or str(uuid.uuid4()), answer=answer, error=error,
            outcome="error" if error else "answered" if answer is not None else "unsupported",
            interpreted_task=task, model_calls=0, tool_calls=calls, total_tokens=0,
            elapsed_seconds=round(time.monotonic() - started, 3),
            table_pages=self.tools.table.pages_read, table_bytes=self.tools.table.bytes_read,
            limitation_kind="capability_gap" if task is None else None,
            limitation_reason="Question is outside the reviewed logistics contract" if task is None else None,
            capability_request=request,
        )
