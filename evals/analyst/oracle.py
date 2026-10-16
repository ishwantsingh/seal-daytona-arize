"""Protected reference calculation for the structured inventory task family."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from self_heal.settings import AnalystConfig


ORACLE_VERSION = "inventory-totals-v1"


class OracleError(ValueError):
    pass


def reference_answer(rows: Iterable[Mapping[str, Any]], task: Mapping[str, Any], config: AnalystConfig) -> dict[str, Any]:
    """Return the exact answer without calling model-facing harness code."""
    if config.evaluation.oracle_version != ORACLE_VERSION:
        raise OracleError("Configured oracle version is unavailable")
    _validate_task(task, config)
    materialized_rows = [dict(row) for row in rows]
    _validate_rows(materialized_rows, config)

    filter_field = task.get("filter_field")
    filter_value = task.get("filter_value")
    matching_rows = (
        materialized_rows
        if filter_field is None
        else [row for row in materialized_rows if row[filter_field] == filter_value]
    )
    metric = task["metric"]
    group_by = task.get("group_by")
    if group_by is None:
        return {"value": sum(_metric_value(row, metric) for row in matching_rows)}

    totals: dict[str, int] = defaultdict(int)
    for row in matching_rows:
        totals[row[group_by]] += _metric_value(row, metric)
    return {"groups": {name: totals[name] for name in sorted(totals)}}


def _validate_task(task: Mapping[str, Any], config: AnalystConfig) -> None:
    allowed = {"metric", "filter_field", "filter_value", "group_by"}
    if not isinstance(task, Mapping) or set(task) - allowed:
        raise OracleError("Task contains unsupported fields")
    if task.get("metric") not in config.metrics:
        raise OracleError("Task metric is unsupported")
    filter_field = task.get("filter_field")
    filter_value = task.get("filter_value")
    if (filter_field is None) != (filter_value is None):
        raise OracleError("Filter field and value must be provided together")
    if filter_field is not None and (filter_field not in config.filter_fields or not isinstance(filter_value, str)):
        raise OracleError("Task filter is unsupported")
    group_by = task.get("group_by")
    if group_by is not None and group_by not in config.group_fields:
        raise OracleError("Task grouping field is unsupported")


def _validate_rows(rows: list[dict[str, Any]], config: AnalystConfig) -> None:
    schema = config.table_schema
    seen_skus: set[str] = set()
    for row in rows:
        if set(row) != set(schema):
            raise OracleError("Row fields do not match schema")
        for field, kind in schema.items():
            value = row[field]
            if kind == "string" and (not isinstance(value, str) or not value):
                raise OracleError(f"{field} must be a nonempty string")
            if kind == "integer" and (type(value) is not int or value < 0):
                raise OracleError(f"{field} must be a nonnegative integer")
        if row["reserved"] > row["on_hand"]:
            raise OracleError("reserved cannot exceed on_hand")
        if row["sku"] in seen_skus:
            raise OracleError("sku must be unique within a dataset")
        seen_skus.add(row["sku"])


def _metric_value(row: Mapping[str, Any], metric: str) -> int:
    if metric == "available":
        return row["on_hand"] - row["reserved"]
    return row[metric]
