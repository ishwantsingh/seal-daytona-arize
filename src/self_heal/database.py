"""SQLite JSON repositories with real indexes and atomic compare-and-swap writes.

A small document interface keeps domain repositories independent of persistence.
Queries compile to parameterized SQL; unsupported operators fail explicitly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ASCENDING, DESCENDING = 1, -1
DatabaseError = sqlite3.DatabaseError
DuplicateKeyError = sqlite3.IntegrityError


class ReturnDocument:
    AFTER = True


def _encode(value):
    if isinstance(value, datetime):
        return {"__seal_datetime__": value.isoformat()}
    raise TypeError(f"Unsupported database value: {type(value).__name__}")


def _decode(value):
    if set(value) == {"__seal_datetime__"}:
        return datetime.fromisoformat(value["__seal_datetime__"])
    return value


def _dump(value):
    return json.dumps(value, default=_encode, separators=(",", ":"), sort_keys=True)


def _path(field):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", field):
        raise ValueError("Invalid repository field")
    return "$." + field


def _expr(field):
    path = _path(field)
    return f"COALESCE(json_extract(document, '{path}.__seal_datetime__'), json_extract(document, '{path}'))"


def _where(query):
    clauses, args = [], []
    for field, value in query.items():
        if field in {"$or", "$and"}:
            sub = [_where(item) for item in value]
            clauses.append(
                "("
                + (" OR " if field == "$or" else " AND ").join(x[0] for x in sub)
                + ")"
            )
            args.extend(arg for _, vals in sub for arg in vals)
            continue
        expr = _expr(field)
        conditions = value if isinstance(value, dict) else {"$eq": value}
        for op, operand in conditions.items():
            if op == "$exists":
                clauses.append(
                    f"json_type(document, '{_path(field)}') IS {'NOT ' if operand else ''}NULL"
                )
            elif op in {"$in", "$nin"}:
                if not operand:
                    clauses.append("0" if op == "$in" else "1")
                else:
                    clauses.append(
                        f"{expr} {'NOT ' if op == '$nin' else ''}IN ({','.join('?' for _ in operand)})"
                    )
                    args.extend(operand)
            elif op == "$regex":
                clauses.append(f"{expr} REGEXP ?")
                args.append(operand)
            else:
                symbol = {
                    "$eq": "IS",
                    "$ne": "IS NOT",
                    "$gt": ">",
                    "$gte": ">=",
                    "$lt": "<",
                    "$lte": "<=",
                }.get(op)
                if symbol is None:
                    raise ValueError(f"Unsupported repository operator: {op}")
                clauses.append(f"{expr} {symbol} ?")
                args.append(
                    operand.isoformat() if isinstance(operand, datetime) else operand
                )
    return " AND ".join(clauses) or "1", args


def _get(document, field):
    for key in field.split("."):
        if not isinstance(document, dict) or key not in document:
            return None
        document = document[key]
    return document


def _set(document, field, value):
    keys = field.split(".")
    for key in keys[:-1]:
        document = document.setdefault(key, {})
    document[keys[-1]] = value


@dataclass
class WriteResult:
    matched_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    inserted_id: str | None = None


class SQLiteDatabase:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(path), timeout=30, check_same_thread=False, isolation_level=None
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.create_function(
            "regexp",
            2,
            lambda pattern, value: bool(re.search(pattern, str(value or ""))),
            deterministic=True,
        )
        self.lock = threading.RLock()
        self.collections = {}

    def __getitem__(self, name):
        with self.lock:
            if name not in self.collections:
                self.collections[name] = Collection(self, name)
            return self.collections[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.connection.in_transaction
            if not nested:
                self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                if not nested:
                    self.connection.execute("COMMIT")
            except BaseException:
                if not nested:
                    self.connection.execute("ROLLBACK")
                raise

    def list_collection_names(self):
        with self.lock:
            return [
                r[0]
                for r in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]

    def close(self):
        with self.lock:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Collection:
    def __init__(self, database, name):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise ValueError("Invalid collection name")
        self.db, self.table = database, name
        self.database = database
        database.connection.execute(
            f'CREATE TABLE IF NOT EXISTS "{name}" (id TEXT PRIMARY KEY, document TEXT NOT NULL CHECK(json_valid(document)))'
        )

    def create_index(self, fields, *, unique=False, sparse=False):
        if isinstance(fields, str):
            fields = [(fields, 1)]
        name = (
            "idx_"
            + hashlib.sha256(
                _dump([self.table, fields, unique, sparse]).encode()
            ).hexdigest()[:24]
        )
        expressions = ",".join(
            _expr(field) + (" DESC" if direction == -1 else " ASC")
            for field, direction in fields
        )
        where = (
            " WHERE "
            + " AND ".join(
                f"json_type(document, '{_path(f)}') IS NOT NULL" for f, _ in fields
            )
            if sparse
            else ""
        )
        with self.db.lock:
            self.db.connection.execute(
                f'CREATE {"UNIQUE " if unique else ""}INDEX IF NOT EXISTS "{name}" ON "{self.table}" ({expressions}){where}'
            )
        return name

    def insert_one(self, record):
        value = copy.deepcopy(record)
        value.setdefault("_id", str(uuid.uuid4()))
        with self.db.lock:
            self.db.connection.execute(
                f'INSERT INTO "{self.table}" VALUES (?,?)',
                (str(value["_id"]), _dump(value)),
            )
        return WriteResult(inserted_id=value["_id"])

    def insert_many(self, records):
        with self.db.transaction():
            for record in records:
                self.insert_one(record)

    def find(self, query=None, projection=None):
        return Cursor(self, query or {}, projection)

    def find_one(self, query=None, projection=None):
        return next(iter(self.find(query, projection).limit(1)), None)

    def count_documents(self, query):
        where, args = _where(query)
        with self.db.lock:
            return self.db.connection.execute(
                f'SELECT count(*) FROM "{self.table}" WHERE {where}', args
            ).fetchone()[0]

    def update_one(self, query, updates):
        with self.db.transaction():
            before = self.find_one(query)
            if before is None:
                return WriteResult()
            after = copy.deepcopy(before)
            for op, fields in updates.items():
                for field, value in fields.items():
                    if field == "_id":
                        raise ValueError("Record identity is immutable")
                    if op == "$set":
                        _set(after, field, value)
                    elif op == "$inc":
                        _set(after, field, (_get(after, field) or 0) + value)
                    elif op == "$push":
                        _set(after, field, list(_get(after, field) or []) + [value])
                    else:
                        raise ValueError(f"Unsupported update: {op}")
            self.db.connection.execute(
                f'UPDATE "{self.table}" SET document=? WHERE id=?',
                (_dump(after), str(before["_id"])),
            )
            return WriteResult(matched_count=1, modified_count=int(before != after))

    def find_one_and_update(self, query, updates, *, return_document=True):
        with self.db.transaction():
            before = self.find_one(query)
            if before is None:
                return None
            self.update_one(query, updates)
            return self.find_one({"_id": before["_id"]}) if return_document else before

    def delete_many(self, query):
        where, args = _where(query)
        with self.db.lock:
            cur = self.db.connection.execute(
                f'DELETE FROM "{self.table}" WHERE {where}', args
            )
            return WriteResult(deleted_count=cur.rowcount)

    def delete_one(self, query):
        with self.db.transaction():
            record = self.find_one(query)
            return self.delete_many({"_id": record["_id"]}) if record else WriteResult()


class Cursor:
    def __init__(self, collection, query, projection):
        self.collection, self.query, self.projection = collection, query, projection
        self.order, self.maximum = [], None

    def sort(self, field, direction=1):
        self.order = field if isinstance(field, list) else [(field, direction)]
        return self

    def limit(self, maximum):
        if maximum < 0:
            raise ValueError("Limit must be nonnegative")
        self.maximum = maximum or None
        return self

    def __iter__(self):
        where, args = _where(self.query)
        sql = f'SELECT document FROM "{self.collection.table}" WHERE {where}'
        if self.order:
            sql += " ORDER BY " + ",".join(
                _expr(f) + (" DESC" if d == -1 else " ASC") for f, d in self.order
            )
        if self.maximum is not None:
            sql += " LIMIT ?"
            args.append(self.maximum)
        with self.collection.db.lock:
            rows = self.collection.db.connection.execute(sql, args).fetchall()
        for row in rows:
            record = json.loads(row[0], object_hook=_decode)
            if self.projection:
                included = [k for k, v in self.projection.items() if v and k != "_id"]
                if included:
                    record = {k: record[k] for k in included if k in record} | (
                        {"_id": record["_id"]} if self.projection.get("_id", 1) else {}
                    )
                else:
                    record = {
                        k: v for k, v in record.items() if self.projection.get(k, 1)
                    }
            yield record
