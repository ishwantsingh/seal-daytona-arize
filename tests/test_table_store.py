import json
from dataclasses import replace
from pathlib import Path

from self_heal.database import SQLiteDatabase
import pytest

from self_heal.settings import load_config
from self_heal.table_store import TableStore, DatasetError, TableAccessError


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "data" / "small_inventory.json"


def make_store():
    config = load_config()
    database = SQLiteDatabase()
    store = TableStore(database, config)
    store.ensure_indexes()
    rows = json.loads(FIXTURE.read_text())["rows"]
    return store, database, config, rows


def test_materialization_is_idempotent_and_detects_tampering():
    store, database, _, rows = make_store()
    info = store.materialize("small", rows)
    assert info.row_count == len(rows)
    assert len(info.content_hash) == 64
    assert store.materialize("small", rows) == info
    with pytest.raises(DatasetError, match="cannot be changed"):
        store.materialize("small", rows[:-1])
    database.analyst_rows.update_one({"dataset_id": "small", "position": 0}, {"$set": {"row.on_hand": 999}})
    with pytest.raises(DatasetError, match="content changed"):
        store.open_session("small")


def test_dataset_info_is_verified_before_the_supervisor_records_it():
    store, _, _, rows = make_store()
    published = store.materialize("small", rows)
    assert store.dataset_info("small") == published


def test_scoped_pages_filters_and_cursors():
    store, _, config, rows = make_store()
    store.materialize("small", rows)
    store.materialize("other", [{**rows[0], "sku": "OTHER"}])
    table = store.open_session("small")
    assert table.inspect_table()["row_count"] == 6
    assert "dataset_id" not in table.inspect_table()
    assert table.read_rows(cursor="", limit=1)["rows"][0]["sku"] == "A-100"
    first = table.read_rows(limit=2, filter_field="warehouse", filter_value="East")
    assert [row["sku"] for row in first["rows"]] == ["A-100", "C-300"]
    assert first["next_cursor"]
    assert not table.completed_scan("warehouse", "East")
    last = table.read_rows(cursor=first["next_cursor"], limit=2, filter_field="warehouse", filter_value="East")
    assert [row["sku"] for row in last["rows"]] == ["D-400"]
    assert last["next_cursor"] is None
    assert table.completed_scan("warehouse", "East")
    assert not table.completed_scan("warehouse", "West")
    assert table.rows_read == 4
    with pytest.raises(TableAccessError, match="cursor"):
        table.read_rows(cursor=first["next_cursor"], limit=2, filter_field="warehouse", filter_value="West")
    with pytest.raises(TableAccessError, match="cursor"):
        table.read_rows(cursor="invalid!", limit=1)
    with pytest.raises(TableAccessError, match="Page limit"):
        table.read_rows(limit=config.limits.max_page_size + 1)
    with pytest.raises(TableAccessError, match="permitted"):
        table.read_rows(limit=1, filter_field="$where", filter_value="x")


def test_read_budget_is_enforced_outside_harness():
    store, database, config, rows = make_store()
    store.materialize("small", rows)
    limited = replace(config, limits=replace(config.limits, max_pages=1))
    table = TableStore(database, limited).open_session("small")
    table.read_rows(limit=1)
    with pytest.raises(TableAccessError, match="page budget"):
        table.read_rows(limit=1)


def test_invalid_rows_and_unpublished_dataset_are_rejected():
    store, database, _, rows = make_store()
    with pytest.raises(DatasetError, match="reserved"):
        store.materialize("bad", [{**rows[0], "reserved": 100}])
    database.analyst_datasets.insert_one({"_id": "pending", "status": "pending"})
    with pytest.raises(DatasetError, match="unavailable"):
        store.open_session("pending")
