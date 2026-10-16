"""Protected, declarative aggregate contracts independent of candidate code.

New questions select a contract; they never supply executable evaluator code.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

VERSION = "logistics-relational-aggregate-v1"
SCHEMA = {
    "customers": {"customer_id": "TEXT", "segment": "TEXT", "status": "TEXT"},
    "warehouses": {
        "warehouse_id": "TEXT",
        "warehouse_number": "INTEGER",
        "timezone": "TEXT",
    },
    "shipments": {
        "shipment_id": "TEXT",
        "sender_customer_id": "TEXT",
        "origin_warehouse_id": "TEXT",
        "status": "TEXT",
        "sent_at": "TEXT",
    },
}
OPS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def validate_task(task: Any) -> dict[str, Any] | None:
    if not isinstance(task, dict) or set(task) != {
        "operation",
        "relation",
        "aggregate",
        "field",
        "filters",
        "group_by",
    }:
        return None
    relation = task.get("relation")
    if (
        task.get("operation") != "relational_aggregate"
        or not isinstance(relation, str)
        or relation not in SCHEMA
    ):
        return None
    fields = SCHEMA[relation]
    aggregate, field, group = (
        task.get("aggregate"),
        task.get("field"),
        task.get("group_by"),
    )
    if not isinstance(aggregate, str) or aggregate not in {
        "count",
        "count_distinct",
        "sum",
        "avg",
        "min",
        "max",
    }:
        return None
    if field is not None and (not isinstance(field, str) or field not in fields):
        return None
    if aggregate != "count" and field is None:
        return None
    if aggregate in {"sum", "avg", "min", "max"} and fields.get(field) != "INTEGER":
        return None
    if group is not None and (not isinstance(group, str) or group not in fields):
        return None
    filters = task.get("filters")
    if not isinstance(filters, list) or len(filters) > 16:
        return None
    for item in filters:
        if not isinstance(item, dict) or set(item) != {"field", "operator", "value"}:
            return None
        if (
            not isinstance(item["field"], str)
            or item["field"] not in fields
            or not isinstance(item["operator"], str)
            or item["operator"] not in OPS
        ):
            return None
        value = item["value"]
        if (
            type(value) not in {str, int, float}
            or isinstance(value, float)
            and not math.isfinite(value)
        ):
            return None
        if fields[item["field"]] == "INTEGER" and type(value) not in {int, float}:
            return None
    return {**task, "filters": [dict(item) for item in filters]}


def reference_answer(bundle: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """Use a separate SQL implementation to grade generated Python tools."""
    task = validate_task(task)
    if task is None:
        raise ValueError("Invalid relational aggregate contract")
    relation, field = task["relation"], task["field"]
    columns = SCHEMA[relation]
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            f"CREATE TABLE data ({', '.join(f'{name} {kind}' for name, kind in columns.items())})"
        )
        connection.executemany(
            f"INSERT INTO data VALUES ({', '.join('?' for _ in columns)})",
            [tuple(row[name] for name in columns) for row in bundle[relation]],
        )
        aggregate = task["aggregate"]
        expression = (
            f"count(DISTINCT {field})"
            if aggregate == "count_distinct"
            else f"{aggregate}({field or '*'})"
        )
        where = (
            " AND ".join(
                f"{item['field']} {OPS[item['operator']]} ?" for item in task["filters"]
            )
            or "1"
        )
        values = [item["value"] for item in task["filters"]]
        group = task["group_by"]
        if group:
            rows = connection.execute(
                f"SELECT {group}, {expression} FROM data WHERE {where} GROUP BY {group}",
                values,
            )
            return {
                "groups": {
                    str(key): value if value is not None else 0 for key, value in rows
                }
            }
        value = connection.execute(
            f"SELECT {expression} FROM data WHERE {where}", values
        ).fetchone()[0]
        return {"value": value if value is not None else 0}
