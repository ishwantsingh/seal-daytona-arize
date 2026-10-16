"""The three model-facing tools in the initial editable harness."""

from __future__ import annotations

from typing import Any

from self_heal.settings import AnalystConfig
from self_heal.table_store import TableSession


class ToolError(ValueError):
    pass


class AnalystTools:
    def __init__(self, table: TableSession, config: AnalystConfig) -> None:
        self.table = table
        self.config = config

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "inspect_table",
                    "description": "Inspect the schema and row count of the table assigned to this run.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_rows",
                    "description": "Read one bounded page of rows from the assigned table, optionally applying one permitted equality filter. Continue with next_cursor until it is null.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cursor": {"type": "string", "description": "Opaque next_cursor from the previous page. Omit or use an empty string for the first page."},
                            "limit": {"type": "integer", "minimum": 1, "maximum": self.config.limits.max_page_size},
                            "filter_field": {"type": "string", "enum": list(self.config.filter_fields)},
                            "filter_value": {"type": "string"},
                        },
                        "required": ["limit"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": "Calculate the sum of a bounded list of integers, or the difference of exactly two integers. It cannot read table rows.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": ["sum", "difference"]},
                            "values": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "minItems": 1,
                                "maxItems": self.config.limits.max_calculator_operands,
                            },
                        },
                        "required": ["operation", "values"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "inspect_table":
            if arguments:
                raise ToolError("inspect_table takes no arguments")
            return self.table.inspect_table()
        if name == "read_rows":
            if set(arguments) - {"cursor", "limit", "filter_field", "filter_value"}:
                raise ToolError("Unsupported read_rows argument")
            return self.table.read_rows(**arguments)
        if name == "calculate":
            if set(arguments) != {"operation", "values"}:
                raise ToolError("calculate requires operation and values")
            operation, values = arguments["operation"], arguments["values"]
            if not isinstance(values, list) or not 1 <= len(values) <= self.config.limits.max_calculator_operands:
                raise ToolError("Calculator operand count is outside the configured range")
            if any(type(value) is not int for value in values):
                raise ToolError("Calculator accepts only integers")
            if operation == "sum":
                return {"value": sum(values)}
            if operation == "difference" and len(values) == 2:
                return {"value": values[0] - values[1]}
            raise ToolError("Unsupported calculator operation or operand count")
        raise ToolError("Tool is not registered")
