"""Protected checks for the reviewed logistics tool against the independent oracle."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from evals.logistics.generator import public_incident_bundle
from evals.logistics.oracle import ORACLE_VERSION, reference_answer
from harness.logistics import LogisticsAgent, LogisticsTools
from self_heal.contracts import (
    build_eval_case_record, build_evaluation_record, canonical_hash,
    new_trial_id, stable_case_id, utc_now,
)
from self_heal.execution import RunExecutor
from self_heal.logistics_store import LogisticsDatasetStore
from self_heal.settings import AnalystConfig
from self_heal.storage import HistoryStore
from self_heal.telemetry import PhoenixTelemetry


class _NoModel:
    model = "reviewed-logistics-parser"

    def complete(self, *_args: Any) -> None:
        raise RuntimeError("The reviewed logistics tool must not call a model")


def run_logistics_checks(
    store: LogisticsDatasetStore, history: HistoryStore,
    config: AnalystConfig, telemetry: PhoenixTelemetry,
) -> list[dict[str, Any]]:
    contract = (config.task_contracts or {})["logistics-shipment-threshold-v1"]
    scoped = replace(config, task_family="logistics-shipment-threshold",
                     task_contract_version="logistics-shipment-threshold-v1",
                     contract_hash=canonical_hash(contract))
    bundle = public_incident_bundle()
    dataset = store.materialize("logistics-shipment-threshold-public-v1", **bundle)
    results: list[dict[str, Any]] = []
    for warehouse, threshold in ((3, 15), (3, 16), (3, 17), (2, 1)):
        task = {"operation": "count_customers_with_shipment_count_gt",
                "warehouse_number": warehouse, "relative_day": "yesterday", "threshold": threshold}
        question = f"How many customers sent more than {threshold} shipments from warehouse {warehouse} yesterday?"
        expected = reference_answer(bundle, task)
        scenario_id = f"logistics-warehouse-{warehouse}-threshold-{threshold}"
        case_id = stable_case_id(scenario_id=scenario_id, dataset=dataset, task=task,
                                 oracle_version=ORACLE_VERSION)
        history.record_eval_case(build_eval_case_record(
            case_id=case_id, scenario_id=scenario_id, dataset=dataset, task=task,
            expected_answer=expected, oracle_version=ORACLE_VERSION, config=scoped,
            exposure_role="reviewed_tool_check", created_at=utc_now()))
        execution = RunExecutor(history=history, telemetry=telemetry, config=scoped).run(
            model=_NoModel(), tools=LogisticsTools(store.open_session(dataset.dataset_id), scoped),
            dataset=dataset, invocation=question, case_id=case_id,
            case_exposure="reviewed_tool_check", agent_factory=LogisticsAgent)
        result = execution.result
        passed = (result.outcome == "answered" and result.answer == expected
                  and result.tool_calls == 1 and execution.history_status == "recorded")
        trial_id = new_trial_id()
        record = build_evaluation_record(
            evaluation_id=trial_id, case_id=case_id, result=result, passed=passed,
            violation=None if passed else "wrong_or_missing_answer", dataset=dataset,
            trace=execution.trace, config=scoped, created_at=utc_now())
        record["role"] = "Logistics tool check"
        history.record_evaluation(record)
        results.append({"case_id": case_id, "run_id": result.run_id, "question": question,
                        "expected": expected, "actual": result.answer, "passed": passed,
                        "tool_calls": result.tool_calls, "table_pages": result.table_pages})
    return results
