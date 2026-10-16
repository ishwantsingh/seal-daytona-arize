from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3

import pytest

from self_heal.database import SQLiteDatabase
from self_heal.storage import HistoryStore


def test_reopen_preserves_datetime_types_and_nested_indexed_queries(tmp_path):
    path = tmp_path / "state.sqlite3"
    stamp = datetime.now(timezone.utc)
    with SQLiteDatabase(path) as db:
        db.runs.create_index([("dataset.id", 1), ("created_at", -1)])
        db.runs.insert_one(
            {"_id": "run-1", "dataset": {"id": "data-1"}, "created_at": stamp}
        )
    with SQLiteDatabase(path) as db:
        record = db.runs.find_one(
            {"dataset.id": "data-1", "created_at": {"$gte": stamp}}
        )
        assert record["created_at"] == stamp
        assert isinstance(record["created_at"], datetime)
        assert db.runs.count_documents({"dataset.id": "other"}) == 0


def test_unique_indexes_survive_restart_and_sparse_fields_allow_missing_values(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    with SQLiteDatabase(path) as db:
        db.candidates.create_index("commit", unique=True, sparse=True)
        db.candidates.insert_many(
            [{"_id": "a"}, {"_id": "b"}, {"_id": "c", "commit": "sha"}]
        )
    with SQLiteDatabase(path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.candidates.insert_one({"_id": "d", "commit": "sha"})
        assert db.candidates.count_documents({}) == 3


def test_transaction_rolls_back_partial_batch():
    with SQLiteDatabase() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.runs.insert_many(
                [{"_id": "same", "value": 1}, {"_id": "same", "value": 2}]
            )
        assert db.runs.count_documents({}) == 0


def test_compare_and_swap_has_one_winner_across_independent_connections(tmp_path):
    path = tmp_path / "state.sqlite3"
    with SQLiteDatabase(path) as db:
        history = HistoryStore(db)
        history.ensure_indexes()
        assert history.compare_and_swap_active(
            task_family="inventory",
            expected_parent="initial",
            commit="parent",
            evidence_id="base",
            environment_hash="env",
            at=datetime.now(timezone.utc),
        )

    def attempt(index):
        with SQLiteDatabase(path) as db:
            return HistoryStore(db).compare_and_swap_active(
                task_family="inventory",
                expected_parent="parent",
                commit=f"child-{index}",
                evidence_id=f"e-{index}",
                environment_hash="env",
                at=datetime.now(timezone.utc),
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1


def test_conditional_update_cannot_overwrite_a_completed_record():
    with SQLiteDatabase() as db:
        db.runs.insert_one({"_id": "run", "status": "running", "lifecycle": []})
        first = db.runs.update_one(
            {"_id": "run", "status": "running"},
            {"$set": {"status": "completed"}, "$push": {"lifecycle": "done"}},
        )
        second = db.runs.update_one(
            {"_id": "run", "status": "running"}, {"$set": {"status": "bad"}}
        )
        assert first.matched_count == 1 and second.matched_count == 0
        assert db.runs.find_one({"_id": "run"})["lifecycle"] == ["done"]


def test_missing_field_distinguished_from_explicit_null():
    with SQLiteDatabase() as db:
        db.runs.insert_many(
            [
                {"_id": "missing"},
                {"_id": "null", "value": None},
                {"_id": "set", "value": 1},
            ]
        )
        assert db.runs.count_documents({"value": {"$exists": False}}) == 1
        assert db.runs.count_documents({"value": {"$exists": True}}) == 2
        assert (
            db.runs.count_documents(
                {"$or": [{"value": 1}, {"_id": {"$regex": "^miss"}}]}
            )
            == 2
        )


def test_unsupported_query_operators_and_sql_identifier_injection_fail():
    with SQLiteDatabase() as db:
        with pytest.raises(ValueError):
            db.runs.find_one({"field": {"$where": "anything"}})
        with pytest.raises(ValueError):
            db.runs.find_one({"value'); DROP TABLE runs; --": 1})


def test_array_exposure_query_retains_accepted_incidents_as_regressions():
    with SQLiteDatabase() as db:
        db.eval_cases.insert_many(
            [
                {
                    "_id": "accepted",
                    "origin": "observed_incident",
                    "exposures": [{"role": "original"}, {"role": "regression"}],
                },
                {
                    "_id": "unaccepted",
                    "origin": "observed_incident",
                    "exposures": [{"role": "original"}],
                },
            ]
        )
        rows = list(
            db.eval_cases.find(
                {"origin": "observed_incident", "exposures.role": "regression"}
            )
        )
        assert [row["_id"] for row in rows] == ["accepted"]
