"""Independent protected oracle for logistics-shipment-threshold-v1."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ORACLE_VERSION = "logistics-shipment-threshold-v1"


class LogisticsOracleError(ValueError):
    pass


def reference_answer(bundle: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, int]:
    """Compute the frozen answer without importing harness or store code."""
    required = {"operation", "warehouse_number", "relative_day", "threshold"}
    if set(task) != required or task.get("operation") != "count_customers_with_shipment_count_gt" or task.get("relative_day") != "yesterday":
        raise LogisticsOracleError("Unsupported logistics task")
    if type(task["warehouse_number"]) is not int or type(task["threshold"]) is not int or task["threshold"] < 0:
        raise LogisticsOracleError("Invalid logistics task values")
    try:
        reference = datetime.fromisoformat(str(bundle["reference_instant"]).replace("Z", "+00:00"))
        if reference.tzinfo is None: raise ValueError
        zone = ZoneInfo(bundle["reporting_timezone"])
    except (KeyError, ValueError, TypeError) as exc:
        raise LogisticsOracleError("Invalid bundle temporal context") from exc
    warehouses = {row["warehouse_id"]: row["warehouse_number"] for row in bundle["warehouses"]}
    local_ref = reference.astimezone(zone)
    end = local_ref.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    counts: Counter[str] = Counter()
    for shipment in bundle["shipments"]:
        sent_at = datetime.fromisoformat(str(shipment["sent_at"]).replace("Z", "+00:00"))
        if (shipment["status"] == "sent" and warehouses.get(shipment["origin_warehouse_id"]) == task["warehouse_number"]
                and start <= sent_at.astimezone(zone) < end):
            counts[shipment["sender_customer_id"]] += 1
    return {"value": sum(count > task["threshold"] for count in counts.values())}
