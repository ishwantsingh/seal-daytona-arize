"""Reviewed logistics capability built on the narrow, dataset-scoped session."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from harness.agent import RunResult, run_question
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
        return [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": "Count distinct customers with more than a threshold of sent shipments from a numbered warehouse yesterday in the assigned bundle.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "warehouse_number": {"type": "integer"},
                            "relative_day": {"type": "string", "enum": ["yesterday"]},
                            "threshold": {"type": "integer", "minimum": 0},
                        },
                        "required": ["warehouse_number", "relative_day", "threshold"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != TOOL_NAME or set(arguments) != {
            "warehouse_number",
            "relative_day",
            "threshold",
        }:
            raise ToolError("Logistics tool or arguments are unsupported")
        warehouse, day, threshold = (
            arguments["warehouse_number"],
            arguments["relative_day"],
            arguments["threshold"],
        )
        if (
            type(warehouse) is not int
            or warehouse < 1
            or day != "yesterday"
            or type(threshold) is not int
            or threshold < 0
        ):
            raise ToolError("Invalid logistics task values")
        counts: Counter[str] = Counter()
        cursor: str | None = None
        started = time.monotonic()
        for _ in range(self.config.limits.max_pages):
            if time.monotonic() - started > self.config.limits.max_elapsed_seconds:
                raise ToolError("Logistics scan exceeded the time budget")
            page = self.table.read_shipments(
                warehouse_number=warehouse,
                relative_day=day,
                cursor=cursor,
                limit=self.config.limits.max_page_size,
            )
            if self.table.bytes_read > self.config.limits.max_bytes:
                raise ToolError("Logistics scan exceeded the byte budget")
            for shipment in page["shipments"]:
                counts[shipment["sender_customer_id"]] += 1
            cursor = page["next_cursor"]
            if cursor is None:
                return {
                    "value": sum(count > threshold for count in counts.values()),
                    "shipments_scanned": sum(counts.values()),
                }
        raise ToolError("Logistics scan exceeded the page budget")


def validate_logistics_task(task: Any) -> dict[str, Any] | None:
    """Validate the structured oracle contract, independently of question wording."""
    if not isinstance(task, dict) or set(task) != {
        "operation",
        "warehouse_number",
        "relative_day",
        "threshold",
    }:
        return None
    if (
        task["operation"] != "count_customers_with_shipment_count_gt"
        or type(task["warehouse_number"]) is not int
        or task["warehouse_number"] < 1
        or task["relative_day"] != "yesterday"
        or type(task["threshold"]) is not int
        or task["threshold"] < 0
    ):
        return None
    return dict(task)


def capability_request_for_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "shipment_customer_threshold",
        "domain": "logistics",
        "task_family": "logistics-shipment-threshold",
        "contract_version": "logistics-shipment-threshold-v1",
        **task,
        "required_relations": ["shipments", "warehouses", "customers"],
    }


class LogisticsAgent:
    """Use natural-language model reasoning over the registered logistics tools."""

    def __init__(self, model: Any, tools: Any, config: AnalystConfig) -> None:
        self.model, self.tools, self.config = model, tools, config

    def run(
        self, invocation: dict[str, Any] | str, *, run_id: str | None = None
    ) -> RunResult:
        if isinstance(invocation, str):
            return run_question(
                self.model,
                self.tools,
                self.config,
                invocation,
                run_id=run_id,
                domain="logistics",
            )
        # Structured tool checks bypass interpretation, not natural-language queries.
        import uuid

        started = time.monotonic()
        task = validate_logistics_task(invocation)
        answer = error = None
        calls = 0
        try:
            if task is None:
                raise ToolError("Invalid structured logistics task")
            calls = 1
            result = self.tools.execute(
                TOOL_NAME,
                {
                    key: task[key]
                    for key in ("warehouse_number", "relative_day", "threshold")
                },
            )
            answer = {"value": result["value"]}
        except (ToolError, TableAccessError, KeyError, TypeError) as exc:
            error = str(exc)
        return RunResult(
            run_id or str(uuid.uuid4()),
            answer,
            error,
            "error" if error else "answered",
            task,
            0,
            calls,
            0,
            round(time.monotonic() - started, 3),
            self.tools.table.pages_read,
            self.tools.table.bytes_read,
            capability_request=capability_request_for_task(task) if task else None,
        )
