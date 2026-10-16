"""Bounded, redacted evidence from the actual run-scoped table and tool boundary."""

from __future__ import annotations

import time
from typing import Any

from self_heal.telemetry import redact_trace_payload


def safe_payload(value: Any) -> Any:
    """Keep useful inventory values while masking secret and sensitive fields."""
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if any(part in str(key).lower() for part in
                 ("secret", "password", "token", "api_key", "authorization", "email", "phone", "address"))
                 else safe_payload(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_payload(item) for item in value]
    return redact_trace_payload(value) if isinstance(value, str) else value


class EvidenceTools:
    """Observe tool calls without changing their execution or result."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools
        self.table = tools.table
        self.calls: list[dict[str, Any]] = []

    def definitions(self) -> list[dict[str, Any]]:
        return self._tools.definitions()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        entry: dict[str, Any] = {"name": name, "arguments": safe_payload(arguments)}
        self.calls.append(entry)
        try:
            result = self._tools.execute(name, arguments)
            entry["result"] = safe_payload(result)
            if isinstance(result, dict) and result.get("error"):
                entry["error"] = safe_payload(result["error"])
            return result
        except Exception as exc:
            entry["error"] = type(exc).__name__ + ": " + str(exc)[:200]
            raise
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000, 1)

    def record_remote_tool(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        entry = {"name": name, "arguments": safe_payload(arguments), "result": safe_payload(result),
                 "duration_ms": None}
        if result.get("error"):
            entry["error"] = safe_payload(result["error"])
        self.calls.append(entry)


def run_evidence(table: Any, tools: EvidenceTools) -> dict[str, Any]:
    pages = getattr(table, "evidence_pages", [])
    logistics = getattr(table, "input_kind", None) == "logistics_bundle"
    return {
        "sqlite": {"collection": "logistics_bundle" if logistics else "analyst_rows", "source": getattr(table, "dataset_id", None),
                  "columns": list(getattr(table, "schema", {})), "pages": pages,
                  "row_count": getattr(table, "rows_read", sum(len(page["rows"]) for page in pages))},
        "tool_calls": tools.calls,
    }
