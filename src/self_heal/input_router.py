"""Typed routing for protected inventory tables and logistics bundles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from self_heal.logistics_store import DatasetBundleInfo, LogisticsDatasetStore
from self_heal.table_store import TableStore, DatasetInfo


@dataclass(frozen=True)
class InputDescriptor:
    input_kind: str
    domain: str
    dataset_id: str
    content_hash: str
    row_count: int | None
    relation_counts: dict[str, int] | None = None


class InputRouter:
    """Resolve only an explicitly selected protected input type.

    There is no cross-domain fallback: a logistics ID is never silently opened
    through the inventory table store.
    """

    def __init__(self, tables: TableStore, logistics: LogisticsDatasetStore) -> None:
        self.tables, self.logistics = tables, logistics

    def describe(self, *, input_kind: str, dataset_id: str) -> InputDescriptor:
        if input_kind == "inventory_table":
            info: DatasetInfo = self.tables.dataset_info(dataset_id)
            return InputDescriptor("inventory_table", "inventory", info.dataset_id, info.content_hash, info.row_count)
        if input_kind == "logistics_bundle":
            info: DatasetBundleInfo = self.logistics.dataset_info(dataset_id)
            return InputDescriptor("logistics_bundle", info.domain, info.dataset_id, info.content_hash, None, {name: int(manifest["row_count"]) for name, manifest in info.relations.items()})
        raise ValueError("Unknown input kind")

    def open(self, *, input_kind: str, dataset_id: str) -> Any:
        if input_kind == "inventory_table": return self.tables.open_session(dataset_id)
        if input_kind == "logistics_bundle": return self.logistics.open_session(dataset_id)
        raise ValueError("Unknown input kind")
