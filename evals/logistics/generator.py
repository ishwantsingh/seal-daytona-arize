"""Deterministic synthetic logistics bundles used only by trusted evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def public_incident_bundle() -> dict[str, Any]:
    """Return the 74-shipment hand-checkable public incident fixture.

    Yesterday at warehouse 3: C1 has 16 sent shipments, C2 has 15, and C3
    has 17.  The remaining rows are deliberately irrelevant noise.
    """
    reference = datetime(2026, 9, 26, 16, tzinfo=timezone.utc)
    yesterday = datetime(2026, 9, 25, 16, tzinfo=timezone.utc)
    customers = [{"customer_id": f"C{number}", "segment": "synthetic", "status": "active"} for number in range(1, 9)]
    warehouses = [
        {"warehouse_id": "W1", "warehouse_number": 1, "timezone": "America/New_York"},
        {"warehouse_id": "W2", "warehouse_number": 2, "timezone": "America/New_York"},
        {"warehouse_id": "W3", "warehouse_number": 3, "timezone": "America/New_York"},
    ]
    shipments: list[dict[str, Any]] = []
    def add(customer: str, warehouse: str, status: str, sent_at: datetime, count: int) -> None:
        for _ in range(count):
            shipments.append({"shipment_id": f"S{len(shipments) + 1:03d}", "sender_customer_id": customer, "origin_warehouse_id": warehouse, "status": status, "sent_at": sent_at.isoformat()})
    add("C1", "W3", "sent", yesterday, 16)
    add("C2", "W3", "sent", yesterday + timedelta(hours=1), 15)
    add("C3", "W3", "sent", yesterday + timedelta(hours=2), 17)
    add("C4", "W1", "sent", yesterday, 10)
    add("C5", "W3", "cancelled", yesterday, 6)
    add("C6", "W3", "sent", yesterday - timedelta(days=1), 5)
    add("C7", "W2", "created", yesterday, 3)
    add("C8", "W2", "sent", yesterday, 2)
    assert len(shipments) == 74
    return {"reference_instant": reference.isoformat(), "reporting_timezone": "America/New_York", "customers": customers, "warehouses": warehouses, "shipments": shipments}


def private_evaluation_bundle(seed: int) -> dict[str, Any]:
    """Make a fresh, deterministic logistics bundle for protected selection.

    Candidate code sees this only through a scoped session. Re-keying every
    relation prevents a patch from relying on public fixture identifiers, while
    the bounded additional shipment set varies the expected threshold count.
    """

    if type(seed) is not int or seed < 0:
        raise ValueError("Evaluation seed must be a nonnegative integer")
    base = public_incident_bundle()
    suffix = f"P{seed:08x}"
    customer_ids = {row["customer_id"]: f"{suffix}-{row['customer_id']}" for row in base["customers"]}
    warehouse_ids = {row["warehouse_id"]: f"{suffix}-{row['warehouse_id']}" for row in base["warehouses"]}
    customers = [{**row, "customer_id": customer_ids[row["customer_id"]], "segment": "private"}
                 for row in base["customers"]]
    warehouses = [{**row, "warehouse_id": warehouse_ids[row["warehouse_id"]]}
                  for row in base["warehouses"]]
    shipments = [
        {
            **row,
            "shipment_id": f"{suffix}-S{index:03d}",
            "sender_customer_id": customer_ids[row["sender_customer_id"]],
            "origin_warehouse_id": warehouse_ids[row["origin_warehouse_id"]],
        }
        for index, row in enumerate(base["shipments"], start=1)
    ]
    extra = seed % 7 + 1
    for index in range(extra):
        shipments.append({
            "shipment_id": f"{suffix}-X{index:03d}",
            "sender_customer_id": customer_ids["C4"],
            "origin_warehouse_id": warehouse_ids["W3"],
            "status": "sent", "sent_at": base["shipments"][0]["sent_at"],
        })
    return {**base, "customers": customers, "warehouses": warehouses, "shipments": shipments}
