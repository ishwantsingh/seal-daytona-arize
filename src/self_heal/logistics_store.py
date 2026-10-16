"""Trusted immutable storage and bounded reads for logistics bundles.

This module deliberately does not reuse ``analyst_rows``.  A logistics input is
an immutable, related-data snapshot and the model-facing capability exposes
only pre-scoped shipment pages; it never exposes database queries or repositories.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Mapping

from self_heal.database import ASCENDING
from self_heal.database import SQLiteDatabase as Database

from self_heal.table_store import DatasetError, TableAccessError


RELATIONS = ("customers", "warehouses", "shipments")
_SHIPMENT_STATUSES = {"created", "sent", "cancelled"}


@dataclass(frozen=True)
class DatasetBundleInfo:
    dataset_id: str
    domain: str
    schema_version: str
    content_hash: str
    reporting_timezone: str
    reference_instant: datetime
    relations: Mapping[str, Mapping[str, Any]]

    @property
    def input_kind(self) -> str:
        return "logistics_bundle"

    @property
    def row_count(self) -> int:
        return int(self.relations["shipments"]["row_count"])


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _relation_hash(rows: list[dict[str, Any]]) -> str:
    return _canonical_hash(rows)


class LogisticsDatasetStore:
    """Supervisor-owned logistics bundle store with application-level immutability."""

    def __init__(self, database: Database) -> None:
        self._datasets = database["logistics_datasets"]
        self._customers = database["logistics_customers"]
        self._warehouses = database["logistics_warehouses"]
        self._shipments = database["logistics_shipments"]

    def ensure_indexes(self) -> None:
        for collection, key in ((self._customers, "customer_id"), (self._warehouses, "warehouse_id"), (self._shipments, "shipment_id")):
            collection.create_index([("dataset_id", ASCENDING), (key, ASCENDING)], unique=True)
            collection.create_index([("dataset_id", ASCENDING), ("position", ASCENDING)], unique=True)
        self._shipments.create_index([
            ("dataset_id", ASCENDING), ("origin_warehouse_id", ASCENDING),
            ("status", ASCENDING), ("sent_at", ASCENDING),
        ])

    def materialize(
        self, dataset_id: str, *, customers: list[dict[str, Any]], warehouses: list[dict[str, Any]],
        shipments: list[dict[str, Any]], reference_instant: datetime | str, reporting_timezone: str,
    ) -> DatasetBundleInfo:
        if not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 100:
            raise DatasetError("Invalid dataset ID")
        reference = _utc_datetime(reference_instant, "reference_instant")
        try:
            ZoneInfo(reporting_timezone)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise DatasetError("Invalid reporting timezone") from exc
        relations = {"customers": [dict(row) for row in customers], "warehouses": [dict(row) for row in warehouses], "shipments": [_canonical_shipment(row) for row in shipments]}
        self._validate(relations, reference)
        manifest = {name: {"row_count": len(rows), "content_hash": _relation_hash(rows)} for name, rows in relations.items()}
        content_hash = _canonical_hash({
            "domain": "logistics", "schema_version": "logistics-snapshot-v1",
            "reference_instant": reference.isoformat(), "reporting_timezone": reporting_timezone,
            "relations": manifest,
        })
        existing = self._datasets.find_one({"_id": dataset_id})
        if existing:
            if existing.get("status") != "ready":
                raise DatasetError("Dataset materialization is incomplete")
            if existing.get("content_hash") != content_hash:
                raise DatasetError("Published dataset cannot be changed")
            self._verify(existing)
            return self._info(existing)
        metadata = {
            "_id": dataset_id, "status": "pending", "domain": "logistics", "schema_version": "logistics-snapshot-v1",
            "reference_instant": reference, "reporting_timezone": reporting_timezone, "relations": manifest,
            "content_hash": content_hash,
        }
        self._datasets.insert_one(metadata)
        try:
            for name, collection in (("customers", self._customers), ("warehouses", self._warehouses), ("shipments", self._shipments)):
                rows = relations[name]
                if rows:
                    collection.insert_many([{"dataset_id": dataset_id, "position": position, **_storage_row(name, row)} for position, row in enumerate(rows)])
            self._verify(metadata)
            if self._datasets.update_one({"_id": dataset_id, "status": "pending"}, {"$set": {"status": "ready"}}).modified_count != 1:
                raise DatasetError("Dataset publication failed")
        except Exception:
            for collection in (self._customers, self._warehouses, self._shipments):
                collection.delete_many({"dataset_id": dataset_id})
            self._datasets.delete_one({"_id": dataset_id, "status": "pending"})
            raise
        return self._info(metadata)

    def dataset_info(self, dataset_id: str) -> DatasetBundleInfo:
        metadata = self._datasets.find_one({"_id": dataset_id, "status": "ready"})
        if not metadata:
            raise DatasetError("Dataset is unavailable")
        self._verify(metadata)
        return self._info(metadata)

    def list_dataset_info(self) -> list[DatasetBundleInfo]:
        return [self.dataset_info(item["_id"]) for item in self._datasets.find({"status": "ready"}, {"_id": 1}).sort("_id", ASCENDING)]

    def open_session(self, dataset_id: str) -> "LogisticsSession":
        return LogisticsSession(self._shipments, self._warehouses, self.dataset_info(dataset_id))

    def verified_relations(self, dataset_id: str) -> dict[str, list[dict[str, Any]]]:
        self.dataset_info(dataset_id)
        result: dict[str, list[dict[str, Any]]] = {}
        for name, collection in (("customers", self._customers), ("warehouses", self._warehouses), ("shipments", self._shipments)):
            result[name] = [
                _canonical_document(name, doc)
                for doc in collection.find({"dataset_id": dataset_id}).sort("position", ASCENDING)
            ]
        return result

    def _verify(self, metadata: Mapping[str, Any]) -> None:
        dataset_id = metadata["_id"]
        observed: dict[str, dict[str, Any]] = {}
        for name, collection in (("customers", self._customers), ("warehouses", self._warehouses), ("shipments", self._shipments)):
            docs = list(collection.find({"dataset_id": dataset_id}).sort("position", ASCENDING))
            if [doc["position"] for doc in docs] != list(range(metadata["relations"][name]["row_count"])):
                raise DatasetError("Stored relation row count or order changed")
            rows = [_canonical_document(name, doc) for doc in docs]
            observed[name] = {"row_count": len(rows), "content_hash": _relation_hash(rows)}
        if observed != metadata["relations"]:
            raise DatasetError("Stored dataset content changed")
        expected = _canonical_hash({
            "domain": metadata["domain"], "schema_version": metadata["schema_version"],
            "reference_instant": _stored_utc_datetime(metadata["reference_instant"], "reference_instant").isoformat(),
            "reporting_timezone": metadata["reporting_timezone"], "relations": observed,
        })
        if expected != metadata["content_hash"]:
            raise DatasetError("Stored dataset content changed")

    @staticmethod
    def _info(metadata: Mapping[str, Any]) -> DatasetBundleInfo:
        return DatasetBundleInfo(metadata["_id"], metadata["domain"], metadata["schema_version"], metadata["content_hash"], metadata["reporting_timezone"], _stored_utc_datetime(metadata["reference_instant"], "reference_instant"), metadata["relations"])

    @staticmethod
    def _validate(relations: Mapping[str, list[dict[str, Any]]], reference: datetime) -> None:
        if set(relations) != set(RELATIONS) or any(not isinstance(rows, list) for rows in relations.values()):
            raise DatasetError("Logistics relations are malformed")
        customers, warehouses, shipments = relations["customers"], relations["warehouses"], relations["shipments"]
        customer_ids = _unique_rows(customers, "customer_id", {"customer_id", "segment", "status"})
        warehouse_ids = _unique_rows(warehouses, "warehouse_id", {"warehouse_id", "warehouse_number", "timezone"})
        if len({row["warehouse_number"] for row in warehouses}) != len(warehouses) or any(type(row["warehouse_number"]) is not int for row in warehouses):
            raise DatasetError("warehouse_number must be unique integer")
        for row in warehouses:
            try: ZoneInfo(row["timezone"])
            except (TypeError, ZoneInfoNotFoundError) as exc: raise DatasetError("Invalid warehouse timezone") from exc
        _unique_rows(shipments, "shipment_id", {"shipment_id", "sender_customer_id", "origin_warehouse_id", "status", "sent_at"})
        for row in shipments:
            if row["sender_customer_id"] not in customer_ids or row["origin_warehouse_id"] not in warehouse_ids:
                raise DatasetError("Shipment foreign key is orphaned")
            if row["status"] not in _SHIPMENT_STATUSES:
                raise DatasetError("Invalid shipment status")
            if _utc_datetime(row["sent_at"], "sent_at") > reference:
                raise DatasetError("sent_at cannot be later than reference_instant")


def _unique_rows(rows: list[dict[str, Any]], key: str, allowed: set[str]) -> set[str]:
    values: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != allowed or not isinstance(row.get(key), str) or not row[key]:
            raise DatasetError("Malformed logistics row")
        if row[key] in values:
            raise DatasetError(f"Duplicate {key}")
        values.add(row[key])
    return values


def _canonical_shipment(row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    if "sent_at" in value:
        value["sent_at"] = _utc_datetime(value["sent_at"], "sent_at").isoformat()
    return value


def _storage_row(relation: str, row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    if relation == "shipments":
        # Normalize UTC timestamps for indexed SQLite range comparisons.
        value["sent_at"] = _utc_datetime(value["sent_at"], "sent_at").replace(tzinfo=None)
    return value


def _canonical_document(relation: str, document: Mapping[str, Any]) -> dict[str, Any]:
    value = {key: item for key, item in document.items() if key not in {"_id", "dataset_id", "position"}}
    if relation == "shipments":
        value["sent_at"] = _stored_utc_datetime(value["sent_at"], "sent_at").isoformat()
    return value


def _utc_datetime(value: datetime | str, name: str) -> datetime:
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc: raise DatasetError(f"Invalid {name}") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DatasetError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stored_utc_datetime(value: datetime | str, name: str) -> datetime:
    """Read a UTC BSON Date.

    Stored UTC timestamps may be naive datetimes even though
    BSON dates are UTC.  Input validation remains strict in ``_utc_datetime``;
    this conversion is only for values read back from the trusted collection.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return _utc_datetime(value, name)


class LogisticsSession:
    """Narrow model-facing logistics capability; all filtering is host-defined."""

    def __init__(self, shipments: Any, warehouses: Any, bundle: DatasetBundleInfo) -> None:
        self._shipments, self._warehouses, self._bundle = shipments, warehouses, bundle
        self._cursor_key = secrets.token_bytes(32)
        self._completed_scans: set[str] = set()
        self.pages_read = self.bytes_read = self.rows_read = 0

    @property
    def input_kind(self) -> str:
        return "logistics_bundle"

    @property
    def dataset_id(self) -> str:
        return self._bundle.dataset_id

    def inspect_catalog(self) -> dict[str, Any]:
        return {"input_kind": self._bundle.input_kind, "domain": self._bundle.domain, "relations": {name: {"row_count": value["row_count"]} for name, value in self._bundle.relations.items()}, "reporting_timezone": self._bundle.reporting_timezone}

    def inspect_relation(self, relation: str) -> dict[str, Any]:
        if relation not in RELATIONS: raise TableAccessError("Unknown relation")
        return {"relation": relation, "row_count": self._bundle.relations[relation]["row_count"]}

    def read_shipments(self, *, warehouse_number: int, relative_day: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
        if type(warehouse_number) is not int or relative_day != "yesterday" or type(limit) is not int or not 1 <= limit <= 100:
            raise TableAccessError("Invalid logistics read")
        warehouse = self._warehouses.find_one({"dataset_id": self._bundle.dataset_id, "warehouse_number": warehouse_number})
        if not warehouse: raise TableAccessError("Warehouse is unavailable in this bundle")
        local_reference = self._bundle.reference_instant.astimezone(ZoneInfo(self._bundle.reporting_timezone))
        end = local_reference.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
        fingerprint = json.dumps([warehouse_number, relative_day], separators=(",", ":"))
        last = self._decode(cursor, fingerprint) if cursor else -1
        docs = list(self._shipments.find({"dataset_id": self._bundle.dataset_id, "origin_warehouse_id": warehouse["warehouse_id"], "status": "sent", "sent_at": {"$gte": start.astimezone(timezone.utc).replace(tzinfo=None), "$lt": end.astimezone(timezone.utc).replace(tzinfo=None)}, "position": {"$gt": last}}, {"_id": 0, "position": 1, "shipment_id": 1, "sender_customer_id": 1}).sort("position", ASCENDING).limit(limit + 1))
        page = docs[:limit]
        rows = [{"shipment_id": doc["shipment_id"], "sender_customer_id": doc["sender_customer_id"]} for doc in page]
        size = len(json.dumps(rows, separators=(",", ":")).encode())
        self.pages_read += 1; self.bytes_read += size; self.rows_read += len(rows)
        next_cursor = self._encode(page[-1]["position"], fingerprint) if len(docs) > limit else None
        if next_cursor is None:
            self._completed_scans.add(fingerprint)
        return {"shipments": rows, "next_cursor": next_cursor}

    def completed_shipment_scan(self, warehouse_number: int, relative_day: str) -> bool:
        """Whether this session read every scoped shipment page for the request.

        This supervisor-only check lets the isolated candidate runner reject a
        guessed aggregate that stopped before the signed cursor reached the
        terminal page. It deliberately takes no candidate-controlled cursor.
        """

        if type(warehouse_number) is not int or relative_day != "yesterday":
            return False
        fingerprint = json.dumps([warehouse_number, relative_day], separators=(",", ":"))
        return fingerprint in self._completed_scans

    def _encode(self, position: int, fingerprint: str) -> str:
        payload = json.dumps({"position": position, "filter": fingerprint}, separators=(",", ":")).encode(); signature = hmac.digest(self._cursor_key, payload, "sha256")
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def _decode(self, cursor: str, fingerprint: str) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)); payload, signature = raw[:-32], raw[-32:]
            data = json.loads(payload)
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, payload, "sha256")) or data["filter"] != fingerprint or type(data["position"]) is not int or data["position"] < 0: raise ValueError
            return data["position"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error) as exc: raise TableAccessError("Invalid page cursor") from exc
