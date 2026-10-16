"""Deterministic, protected scenario generation and SQLite materialization."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evals.analyst.oracle import OracleError, reference_answer
from self_heal.settings import AnalystConfig
from self_heal.table_store import TableStore, DatasetInfo


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    dataset_id: str
    description: str
    task: dict[str, Any]
    question: str | None
    baseline_expectation: str
    fixture: Path | None
    seed: int | None
    row_count: int | None
    warehouses: tuple[str, ...]
    categories: tuple[str, ...]
    on_hand_min: int | None
    on_hand_max: int | None
    invalid_row: str | None


@dataclass(frozen=True)
class PreparedCase:
    scenario: Scenario
    rows: tuple[dict[str, Any], ...]
    expected_answer: dict[str, Any]


@dataclass(frozen=True)
class MaterializedCase:
    case: PreparedCase
    dataset: DatasetInfo


def load_scenarios(path: Path, config: AnalystConfig) -> dict[str, Scenario]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError("Could not read scenarios file") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("scenarios"), list):
        raise ScenarioError("Scenario file must contain version 1 and scenarios")

    scenarios: dict[str, Scenario] = {}
    for item in raw["scenarios"]:
        scenario = _parse_scenario(item, path.parent, config)
        if scenario.scenario_id in scenarios:
            raise ScenarioError("Scenario IDs must be unique")
        scenarios[scenario.scenario_id] = scenario
    required = set(config.evaluation.required_baseline_scenarios)
    if not required.issubset(scenarios):
        raise ScenarioError("Required baseline scenario is missing")
    return scenarios


def generate_rows(scenario: Scenario, config: AnalystConfig) -> list[dict[str, Any]]:
    if scenario.fixture is not None:
        try:
            data = json.loads(scenario.fixture.read_text(encoding="utf-8"))
            rows = data["rows"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ScenarioError("Could not read scenario fixture") from exc
        if not isinstance(rows, list):
            raise ScenarioError("Scenario fixture rows must be a list")
        if data.get("dataset_id") != scenario.dataset_id:
            raise ScenarioError("Scenario fixture dataset ID does not match")
        return [dict(row) for row in rows]

    assert scenario.seed is not None
    assert scenario.row_count is not None
    assert scenario.on_hand_min is not None
    assert scenario.on_hand_max is not None
    rng = random.Random(scenario.seed)
    rows: list[dict[str, Any]] = []
    for position in range(scenario.row_count):
        on_hand = rng.randint(scenario.on_hand_min, scenario.on_hand_max)
        rows.append(
            {
                "sku": f"SKU-{position + 1:05d}",
                "warehouse": rng.choice(scenario.warehouses),
                "category": rng.choice(scenario.categories),
                "on_hand": on_hand,
                "reserved": rng.randint(0, on_hand),
            }
        )
    rng.shuffle(rows)
    if scenario.invalid_row == "reserved_exceeds_on_hand":
        rows[0]["reserved"] = rows[0]["on_hand"] + 1
    return rows


def prepare_case(scenario: Scenario, config: AnalystConfig) -> PreparedCase:
    if scenario.invalid_row is not None:
        raise ScenarioError("Invalid scenarios cannot be prepared as answerable cases")
    rows = generate_rows(scenario, config)
    try:
        answer = reference_answer(rows, scenario.task, config)
    except OracleError as exc:
        raise ScenarioError("Scenario cannot be answered by the protected oracle") from exc
    return PreparedCase(scenario=scenario, rows=tuple(rows), expected_answer=answer)


def materialize_case(store: TableStore, case: PreparedCase) -> MaterializedCase:
    dataset = store.materialize(case.scenario.dataset_id, list(case.rows))
    return MaterializedCase(case=case, dataset=dataset)


def _parse_scenario(raw: Any, directory: Path, config: AnalystConfig) -> Scenario:
    if not isinstance(raw, dict):
        raise ScenarioError("Scenario must be an object")
    allowed = {
        "scenario_id",
        "dataset_id",
        "description",
        "task",
        "question",
        "baseline_expectation",
        "fixture",
        "generation",
    }
    if set(raw) - allowed:
        raise ScenarioError("Scenario contains unsupported fields")
    required = {"scenario_id", "dataset_id", "description", "task", "baseline_expectation"}
    if not required.issubset(raw) or any(not isinstance(raw[name], str) or not raw[name] for name in required - {"task"}):
        raise ScenarioError("Scenario is missing required metadata")
    if not isinstance(raw["task"], dict):
        raise ScenarioError("Scenario task must be an object")
    expectation = raw["baseline_expectation"]
    if expectation not in {"pass", "fails_model_call_budget", "rejected_dataset"}:
        raise ScenarioError("Scenario baseline expectation is unsupported")
    question = raw.get("question")
    if question is not None and (not isinstance(question, str) or not question):
        raise ScenarioError("Scenario question must be a nonempty string")
    fixture = raw.get("fixture")
    generation = raw.get("generation")
    if (fixture is None) == (generation is None):
        raise ScenarioError("Scenario must define exactly one row source")
    if fixture is not None:
        if not isinstance(fixture, str) or not fixture:
            raise ScenarioError("Scenario fixture must be a path")
        if expectation == "rejected_dataset":
            raise ScenarioError("Fixture scenarios must be answerable")
        return Scenario(
            scenario_id=raw["scenario_id"],
            dataset_id=raw["dataset_id"],
            description=raw["description"],
            task=dict(raw["task"]),
            question=question,
            baseline_expectation=expectation,
            fixture=directory / fixture,
            seed=None,
            row_count=None,
            warehouses=(),
            categories=(),
            on_hand_min=None,
            on_hand_max=None,
            invalid_row=None,
        )
    if not isinstance(generation, dict):
        raise ScenarioError("Scenario generation must be an object")
    generation_allowed = {"seed", "row_count", "warehouses", "categories", "on_hand", "invalid_row"}
    if set(generation) - generation_allowed:
        raise ScenarioError("Scenario generation contains unsupported fields")
    generation_required = {"seed", "row_count", "warehouses", "categories", "on_hand"}
    if not generation_required.issubset(generation):
        raise ScenarioError("Scenario generation is incomplete")
    seed, row_count = generation["seed"], generation["row_count"]
    warehouses, categories, bounds = generation["warehouses"], generation["categories"], generation["on_hand"]
    if (
        type(seed) is not int
        or type(row_count) is not int
        or not 1 <= row_count <= config.evaluation.max_generated_rows
        or not _valid_labels(warehouses)
        or not _valid_labels(categories)
        or not isinstance(bounds, list)
        or len(bounds) != 2
        or any(type(value) is not int for value in bounds)
        or not 1 <= bounds[0] <= bounds[1]
    ):
        raise ScenarioError("Scenario generation values are invalid")
    invalid_row = generation.get("invalid_row")
    if invalid_row not in {None, "reserved_exceeds_on_hand"}:
        raise ScenarioError("Unsupported invalid-row scenario")
    if (invalid_row is None and expectation == "rejected_dataset") or (
        invalid_row is not None and expectation != "rejected_dataset"
    ):
        raise ScenarioError("Scenario expectation does not match its row validity")
    return Scenario(
        scenario_id=raw["scenario_id"],
        dataset_id=raw["dataset_id"],
        description=raw["description"],
        task=dict(raw["task"]),
        question=question,
        baseline_expectation=expectation,
        fixture=None,
        seed=seed,
        row_count=row_count,
        warehouses=tuple(warehouses),
        categories=tuple(categories),
        on_hand_min=bounds[0],
        on_hand_max=bounds[1],
        invalid_row=invalid_row,
    )


def _valid_labels(values: Any) -> bool:
    return (
        isinstance(values, list)
        and values
        and len(values) == len(set(values))
        and all(isinstance(value, str) and value for value in values)
    )
