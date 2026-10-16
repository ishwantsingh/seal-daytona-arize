from pathlib import Path

from self_heal.database import SQLiteDatabase
import pytest

from evals.analyst.generator import generate_rows, load_scenarios, materialize_case, prepare_case
from evals.analyst.oracle import OracleError, reference_answer
from self_heal.settings import load_config
from self_heal.table_store import TableStore, DatasetError


SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "scenarios.yaml"


def make_store(config):
    store = TableStore(SQLiteDatabase(), config)
    store.ensure_indexes()
    return store


def test_declared_scenarios_are_deterministic_and_cover_baseline_cases():
    config = load_config()
    scenarios = load_scenarios(SCENARIOS, config)
    assert set(config.evaluation.required_baseline_scenarios).issubset(scenarios)
    bulk = scenarios["bulk-warehouse-available"]
    first = generate_rows(bulk, config)
    second = generate_rows(bulk, config)
    assert first == second
    assert len(first) == 512
    assert {row["warehouse"] for row in first} == {"East", "North", "South", "West"}
    assert [row["sku"] for row in first] != sorted(row["sku"] for row in first)


def test_oracle_matches_hand_checked_small_and_edge_cases():
    config = load_config()
    scenarios = load_scenarios(SCENARIOS, config)
    small = prepare_case(scenarios["small-east-available"], config)
    edge = prepare_case(scenarios["edge-empty-sku"], config)
    assert small.expected_answer == {"value": 18}
    assert edge.expected_answer == {"value": 0}
    rows = [
        {"sku": "A", "warehouse": "East", "category": "Hardware", "on_hand": 10, "reserved": 2},
        {"sku": "B", "warehouse": "West", "category": "Hardware", "on_hand": 7, "reserved": 1},
        {"sku": "C", "warehouse": "East", "category": "Office", "on_hand": 6, "reserved": 4},
    ]
    assert reference_answer(rows, {"metric": "available", "group_by": "warehouse"}, config) == {
        "groups": {"East": 10, "West": 6}
    }


def test_materialization_is_idempotent_and_invalid_rows_are_rejected():
    config = load_config()
    scenarios = load_scenarios(SCENARIOS, config)
    store = make_store(config)
    case = prepare_case(scenarios["edge-empty-sku"], config)
    first = materialize_case(store, case)
    second = materialize_case(store, case)
    assert first.dataset == second.dataset

    invalid = scenarios["invalid-reserved-over-on-hand"]
    invalid_rows = generate_rows(invalid, config)
    with pytest.raises(DatasetError, match="reserved cannot exceed"):
        store.materialize(invalid.dataset_id, invalid_rows)
    with pytest.raises(OracleError, match="reserved cannot exceed"):
        reference_answer(invalid_rows, invalid.task, config)
    with pytest.raises(ValueError, match="Invalid scenarios"):
        prepare_case(invalid, config)
