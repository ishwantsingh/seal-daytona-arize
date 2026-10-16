"""Trusted SQLite storage for immutable analyst tables and scoped reads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from self_heal.database import ASCENDING
from self_heal.database import SQLiteDatabase as Database

from self_heal.settings import AnalystConfig


class DatasetError(ValueError):
    pass


class TableAccessError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetInfo:
    dataset_id: str
    row_count: int
    content_hash: str


def canonical_content_hash(schema: Mapping[str, str], rows: list[dict[str, Any]]) -> str:
    payload = {"schema": dict(schema), "rows": rows}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TableStore:
    def __init__(self, database: Database, config: AnalystConfig) -> None:
        self._datasets = database["analyst_datasets"]
        self._rows = database["analyst_rows"]
        self._config = config

    def ensure_indexes(self) -> None:
        self._rows.create_index([("dataset_id", ASCENDING), ("position", ASCENDING)], unique=True)

    def materialize(self, dataset_id: str, rows: list[dict[str, Any]]) -> DatasetInfo:
        """Publish a validated dataset once; identical retries are read-only."""
        if not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 100:
            raise DatasetError("Invalid dataset ID")
        if not isinstance(rows, list):
            raise DatasetError("Rows must be a list")
        self._validate_rows(rows)
        schema = self._config.table_schema
        content_hash = canonical_content_hash(schema, rows)
        existing = self._datasets.find_one({"_id": dataset_id})
        if existing:
            if existing.get("status") != "ready":
                raise DatasetError("Dataset materialization is incomplete")
            if existing.get("content_hash") != content_hash or existing.get("row_count") != len(rows):
                raise DatasetError("Published dataset cannot be changed")
            self._verify_published(existing)
            return DatasetInfo(dataset_id, len(rows), content_hash)

        self._datasets.insert_one(
            {
                "_id": dataset_id,
                "status": "pending",
                "schema": schema,
                "row_count": len(rows),
                "content_hash": content_hash,
            }
        )
        try:
            if rows:
                self._rows.insert_many(
                    [
                        {"dataset_id": dataset_id, "position": position, "row": dict(row)}
                        for position, row in enumerate(rows)
                    ]
                )
            self._verify_published({"_id": dataset_id, "row_count": len(rows), "content_hash": content_hash, "schema": schema})
            result = self._datasets.update_one({"_id": dataset_id, "status": "pending"}, {"$set": {"status": "ready"}})
            if result.modified_count != 1:
                raise DatasetError("Dataset publication failed")
        except Exception:
            self._rows.delete_many({"dataset_id": dataset_id})
            self._datasets.delete_one({"_id": dataset_id, "status": "pending"})
            raise
        return DatasetInfo(dataset_id, len(rows), content_hash)

    def open_session(self, dataset_id: str) -> TableSession:
        metadata = self._datasets.find_one({"_id": dataset_id, "status": "ready"})
        if not metadata:
            raise DatasetError("Dataset is unavailable")
        self._verify_published(metadata)
        return TableSession(self._rows, metadata, self._config)

    def dataset_info(self, dataset_id: str) -> DatasetInfo:
        """Return verified immutable metadata for a dataset bound to a supervisor run."""

        metadata = self._datasets.find_one({"_id": dataset_id, "status": "ready"})
        if not metadata:
            raise DatasetError("Dataset is unavailable")
        self._verify_published(metadata)
        return DatasetInfo(
            dataset_id=metadata["_id"],
            row_count=metadata["row_count"],
            content_hash=metadata["content_hash"],
        )

    def verified_rows(self, dataset_id: str) -> list[dict[str, Any]]:
        """Protected oracle input; never pass this list to a proposal or runner."""
        self.dataset_info(dataset_id)
        return [doc["row"] for doc in self._rows.find(
            {"dataset_id": dataset_id}, {"_id": 0, "row": 1}
        ).sort("position", ASCENDING)]

    def list_dataset_info(self) -> list[DatasetInfo]:
        """List ready dataset metadata without exposing any table rows.

        The local UI uses this metadata to choose its operator dataset without
        exposing a picker. It deliberately avoids opening a table session, so
        a browser request cannot read data outside a separately authorized
        analyst run.
        """

        records = self._datasets.find(
            {"status": "ready"}, {"_id": 1, "row_count": 1, "content_hash": 1}
        ).sort("_id", 1)
        return [
            DatasetInfo(
                dataset_id=record["_id"],
                row_count=record["row_count"],
                content_hash=record["content_hash"],
            )
            for record in records
        ]

    def _verify_published(self, metadata: Mapping[str, Any]) -> None:
        dataset_id = metadata["_id"]
        documents = list(
            self._rows.find({"dataset_id": dataset_id}, {"_id": 0, "position": 1, "row": 1}).sort("position", ASCENDING)
        )
        positions = [doc["position"] for doc in documents]
        rows = [doc["row"] for doc in documents]
        if positions != list(range(metadata["row_count"])):
            raise DatasetError("Stored dataset row count or order changed")
        if canonical_content_hash(metadata["schema"], rows) != metadata["content_hash"]:
            raise DatasetError("Stored dataset content changed")

    def _validate_rows(self, rows: list[dict[str, Any]]) -> None:
        schema = self._config.table_schema
        seen_skus: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(schema):
                raise DatasetError("Row fields do not match schema")
            for field, kind in schema.items():
                value = row[field]
                if kind == "string" and (not isinstance(value, str) or not value):
                    raise DatasetError(f"{field} must be a nonempty string")
                if kind == "integer" and (type(value) is not int or value < 0):
                    raise DatasetError(f"{field} must be a nonnegative integer")
            if row["reserved"] > row["on_hand"]:
                raise DatasetError("reserved cannot exceed on_hand")
            if row["sku"] in seen_skus:
                raise DatasetError("sku must be unique within a dataset")
            seen_skus.add(row["sku"])


class TableSession:
    """A run-scoped capability. The model sees only method results, never IDs or a client."""

    def __init__(self, rows_collection: Any, metadata: Mapping[str, Any], config: AnalystConfig) -> None:
        self._rows = rows_collection
        self._dataset_id = metadata["_id"]
        self._schema = dict(metadata["schema"])
        self._row_count = metadata["row_count"]
        self._filter_fields = frozenset(config.filter_fields)
        self._limits = config.limits
        self._cursor_key = secrets.token_bytes(32)
        self._completed_queries: set[str] = set()
        self.pages_read = 0
        self.bytes_read = 0
        self.rows_read = 0
        self.evidence_pages: list[dict[str, Any]] = []

    @property
    def dataset_id(self) -> str:
        return self._dataset_id

    @property
    def schema(self) -> dict[str, str]:
        return dict(self._schema)

    def inspect_table(self) -> dict[str, Any]:
        return {
            "fields": self._schema,
            "row_count": self._row_count,
            "filter_fields": sorted(self._filter_fields),
            "max_page_size": self._limits.max_page_size,
        }

    def completed_scan(self, filter_field: str | None, filter_value: str | None) -> bool:
        requested = json.dumps([filter_field, filter_value], separators=(",", ":"))
        unfiltered = json.dumps([None, None], separators=(",", ":"))
        return requested in self._completed_queries or unfiltered in self._completed_queries

    def read_rows(
        self,
        *,
        cursor: str | None = None,
        limit: int,
        filter_field: str | None = None,
        filter_value: str | None = None,
    ) -> dict[str, Any]:
        if cursor == "":
            cursor = None
        if type(limit) is not int or not 1 <= limit <= self._limits.max_page_size:
            raise TableAccessError("Page limit is outside the configured range")
        if self.pages_read >= self._limits.max_pages:
            raise TableAccessError("Table page budget exceeded")
        if filter_field is None:
            if filter_value is not None:
                raise TableAccessError("filter_value requires filter_field")
        elif filter_field not in self._filter_fields or not isinstance(filter_value, str):
            raise TableAccessError("Only permitted string equality filters are supported")

        fingerprint = json.dumps([filter_field, filter_value], separators=(",", ":"))
        last_position = self._decode_cursor(cursor, fingerprint) if cursor is not None else -1
        query: dict[str, Any] = {"dataset_id": self._dataset_id, "position": {"$gt": last_position}}
        if filter_field is not None:
            query[f"row.{filter_field}"] = filter_value
        documents = list(
            self._rows.find(query, {"_id": 0, "position": 1, "row": 1}).sort("position", ASCENDING).limit(limit + 1)
        )
        page = documents[:limit]
        result_rows = [doc["row"] for doc in page]
        size = len(json.dumps(result_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if self.bytes_read + size > self._limits.max_bytes:
            raise TableAccessError("Table byte budget exceeded")
        self.pages_read += 1
        self.bytes_read += size
        self.rows_read += len(result_rows)
        next_cursor = self._encode_cursor(page[-1]["position"], fingerprint) if len(documents) > limit else None
        from self_heal.evidence import safe_payload
        self.evidence_pages.append({
            "number": self.pages_read, "rows": safe_payload(result_rows),
            "row_count": len(result_rows), "has_next": next_cursor is not None,
            "filter": safe_payload({"field": filter_field, "value": filter_value}),
        })
        if next_cursor is None:
            self._completed_queries.add(fingerprint)
        return {"rows": result_rows, "next_cursor": next_cursor}

    def _encode_cursor(self, position: int, fingerprint: str) -> str:
        payload = json.dumps({"position": position, "filter": fingerprint}, separators=(",", ":")).encode("utf-8")
        signature = hmac.digest(self._cursor_key, payload, "sha256")
        return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str, fingerprint: str) -> int:
        if not isinstance(cursor, str) or len(cursor) > 500:
            raise TableAccessError("Invalid page cursor")
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, payload, "sha256")):
                raise ValueError
            data = json.loads(payload)
            if data["filter"] != fingerprint or type(data["position"]) is not int or data["position"] < 0:
                raise ValueError
            return data["position"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
            raise TableAccessError("Invalid page cursor") from exc
