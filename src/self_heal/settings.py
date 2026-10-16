"""Protected configuration and environment loading."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = Path("config/analyst.yaml")


@dataclass(frozen=True)
class Limits:
    max_model_calls: int
    max_tool_calls: int
    max_elapsed_seconds: int
    max_total_tokens: int
    max_page_size: int
    max_pages: int
    max_bytes: int
    max_calculator_operands: int
    context_rounds: int


@dataclass(frozen=True)
class EvaluationConfig:
    oracle_version: str
    scenarios_path: Path
    max_generated_rows: int
    required_baseline_scenarios: tuple[str, ...]
    max_patch_attempts: int = 3
    validation_cases: int = 4
    live_repetitions: int = 2
    max_cost_ratio: float = 2.0
    require_trace: bool = True


@dataclass(frozen=True)
class AnalystConfig:
    task_contract_version: str
    task_family: str
    config_hash: str
    contract_hash: str
    metrics: tuple[str, ...]
    filter_fields: tuple[str, ...]
    group_fields: tuple[str, ...]
    table_schema: dict[str, str]
    limits: Limits
    evaluation: EvaluationConfig
    task_contracts: dict[str, dict[str, Any]] | None = None


@dataclass(frozen=True)
class PhoenixConfig:
    enabled: bool = True
    api_key: str | None = None
    project: str = "seal"
    base_url: str = "http://localhost:6006"
    collector_endpoint: str = "http://localhost:6006/v1/traces"


def load_config(path: Path = DEFAULT_CONFIG) -> AnalystConfig:
    raw_bytes = path.read_bytes()
    raw: dict[str, Any] = yaml.safe_load(raw_bytes.decode("utf-8"))
    limits = Limits(**raw["limits"])
    if any(value <= 0 for value in vars(limits).values()):
        raise ValueError("All analyst limits must be positive")
    schema = dict(raw["table_schema"])
    if not schema or any(kind not in {"string", "integer"} for kind in schema.values()):
        raise ValueError("Unsupported table schema")
    contract = raw["task_contract"]
    version = contract.get("version")
    family = contract.get("family")
    if not isinstance(version, str) or not version or not isinstance(family, str) or not family:
        raise ValueError("Task contract version and family must be configured")
    filters = tuple(contract["filter_fields"])
    groups = tuple(contract["group_fields"])
    if not set(filters + groups).issubset(schema):
        raise ValueError("Filter and grouping fields must exist in the table schema")
    evaluation_raw = raw["evaluation"]
    evaluation = EvaluationConfig(
        oracle_version=evaluation_raw["oracle_version"],
        scenarios_path=Path(evaluation_raw["scenarios_path"]),
        max_generated_rows=evaluation_raw["max_generated_rows"],
        required_baseline_scenarios=tuple(evaluation_raw["required_baseline_scenarios"]),
        max_patch_attempts=evaluation_raw.get("max_patch_attempts", 3),
        validation_cases=evaluation_raw.get("validation_cases", 4),
        live_repetitions=evaluation_raw.get("live_repetitions", 2),
        max_cost_ratio=evaluation_raw.get("max_cost_ratio", 2.0),
        require_trace=evaluation_raw.get("require_trace", True),
    )
    if (
        not evaluation.oracle_version
        or evaluation.max_generated_rows <= 0
        or not evaluation.required_baseline_scenarios
        or any(not scenario_id for scenario_id in evaluation.required_baseline_scenarios)
        or type(evaluation.max_patch_attempts) is not int or evaluation.max_patch_attempts < 1
        or type(evaluation.validation_cases) is not int or evaluation.validation_cases < 3
        or type(evaluation.live_repetitions) is not int or evaluation.live_repetitions < 1
        or type(evaluation.max_cost_ratio) not in {int, float} or evaluation.max_cost_ratio < 1
        or type(evaluation.require_trace) is not bool
    ):
        raise ValueError("Invalid evaluation configuration")
    task_contracts = raw.get("task_contracts", {version: contract})
    if not isinstance(task_contracts, dict) or version not in task_contracts:
        raise ValueError("Task contract registry is invalid")
    for contract_version, registered in task_contracts.items():
        if not isinstance(contract_version, str) or not isinstance(registered, dict) or not registered.get("family"):
            raise ValueError("Task contract registry is invalid")
    return AnalystConfig(
        task_contract_version=version,
        task_family=family,
        config_hash=hashlib.sha256(raw_bytes).hexdigest(),
        contract_hash=hashlib.sha256(
            yaml.safe_dump(contract, sort_keys=True, allow_unicode=True).encode("utf-8")
        ).hexdigest(),
        metrics=tuple(contract["metrics"]),
        filter_fields=filters,
        group_fields=groups,
        table_schema=schema,
        limits=limits,
        evaluation=evaluation,
        task_contracts={str(key): dict(value) for key, value in task_contracts.items()},
    )


def database_path() -> Path:
    return Path(os.environ.get("SEAL_DATABASE_PATH", ".seal/seal.sqlite3")).expanduser()


def agent_model_config() -> tuple[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("OPENROUTER_AGENT_MODEL")
    if not key or not model:
        raise ValueError("OPENROUTER_API_KEY and OPENROUTER_AGENT_MODEL must be set")
    return key, model


def evolution_model_config() -> tuple[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("OPENROUTER_EVOLUTION_MODEL")
    if not key or not model:
        raise ValueError("OPENROUTER_API_KEY and OPENROUTER_EVOLUTION_MODEL must be set")
    return key, model


def phoenix_config() -> PhoenixConfig:
    raw = os.environ.get("PHOENIX_TRACING", "true").strip().lower()
    if raw not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError("PHOENIX_TRACING must be true or false")
    base_url = os.environ.get("PHOENIX_BASE_URL", "http://localhost:6006").rstrip("/")
    return PhoenixConfig(
        enabled=raw in {"true", "1", "yes", "on"},
        api_key=os.environ.get("PHOENIX_API_KEY") or None,
        project=os.environ.get("PHOENIX_PROJECT_NAME", "seal"),
        base_url=base_url,
        collector_endpoint=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", base_url + "/v1/traces"),
    )
