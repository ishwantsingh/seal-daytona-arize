"""One-use, protected final assessment of the exact active harness commit."""

from __future__ import annotations

import json
import os
import random
import secrets
from pathlib import Path
from typing import Any, Callable

from self_heal.database import DuplicateKeyError

from evals.analyst.oracle import reference_answer
from self_heal.evaluation import _resource_violation
from self_heal.contracts import canonical_hash, utc_now, build_evaluation_record, new_trial_id
from self_heal.execution import RunExecution
from self_heal.settings import AnalystConfig
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore, DatasetInfo


class FinalAssessmentError(ValueError):
    pass


def _outside_checkout(path: Path, checkout: Path) -> Path:
    target = path.expanduser().resolve()
    if target.is_relative_to(checkout.resolve()):
        raise FinalAssessmentError("The protected manifest must live outside the editable checkout")
    return target


def reserve_cases(store: TableStore, config: AnalystConfig, *, manifest: Path, checkout: Path) -> dict[str, Any]:
    """Publish fresh immutable SQLite data and write a private, exclusive manifest."""
    target = _outside_checkout(manifest, checkout)
    if target.exists():
        raise FinalAssessmentError("Final manifest already exists; choose a new path")
    target.parent.mkdir(parents=True, exist_ok=True)
    cases = []
    for index, row_count in enumerate((72, 512), start=1):
        seed = secrets.randbits(64)
        rng = random.Random(seed)
        dataset_id = f"final-{secrets.token_hex(10)}"
        rows = []
        for position in range(row_count):
            on_hand = rng.randint(1, 80)
            rows.append({"sku": f"F-{index}-{position:05d}", "warehouse": rng.choice(("East", "West", "North")),
                         "category": rng.choice(("Hardware", "Accessories", "Consumables")),
                         "on_hand": on_hand, "reserved": rng.randint(0, on_hand)})
        dataset = store.materialize(dataset_id, rows)
        task = ({"metric": "available", "filter_field": "warehouse", "filter_value": "East"}
                if index == 1 else {"metric": "available", "group_by": "category"})
        question = ("How many available units are in the East warehouse?" if index == 1
                    else "How many available units are in each category?")
        cases.append({"case_id": "final_" + secrets.token_hex(12), "dataset_id": dataset_id,
                      "dataset_hash": dataset.content_hash, "row_count": dataset.row_count,
                      "task": task, "question": question,
                      "expected_answer_hash": canonical_hash(reference_answer(rows, task, config))})
    document = {"schema_version": 1, "task_family": config.task_family,
                "config_hash": config.config_hash, "oracle_version": config.evaluation.oracle_version,
                "reserved_at": utc_now().isoformat(), "cases": cases}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
    return {"manifest": str(target), "case_count": len(cases), "reserved_at": document["reserved_at"]}


def assess_cases(store: TableStore, history: HistoryStore, config: AnalystConfig, *,
                 manifest: Path, checkout: Path,
                 run: Callable[[DatasetInfo, str, str], RunExecution]) -> list[dict[str, Any]]:
    """Claim each case before execution; preserve failed attempts and prevent reuse."""
    target = _outside_checkout(manifest, checkout)
    document = json.loads(target.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("task_family") != config.task_family or document.get("config_hash") != config.config_hash:
        raise FinalAssessmentError("Final manifest does not match this evaluator configuration")
    if (document.get("oracle_version") != config.evaluation.oracle_version
        or not isinstance(document.get("cases"), list) or not 1 <= len(document["cases"]) <= 10):
        raise FinalAssessmentError("Final manifest is invalid")
    active = history.active_version(config.task_family)
    if not active or not active.get("commit"):
        raise FinalAssessmentError("An accepted active commit is required for final assessment")
    commit = active["commit"]
    results = []
    for case in document["cases"]:
        case_id, dataset_id = case["case_id"], case["dataset_id"]
        if not dataset_id.startswith("final-") or not isinstance(case.get("task"), dict) or not isinstance(case.get("question"), str):
            raise FinalAssessmentError("Final case is invalid")
        dataset = store.dataset_info(dataset_id)
        if dataset.content_hash != case["dataset_hash"] or dataset.row_count != case["row_count"]:
            raise FinalAssessmentError("Final dataset identity changed")
        if history.eval_cases.find_one({"dataset.id": dataset_id}):
            raise FinalAssessmentError("Final dataset was exposed to candidate selection")
        expected = reference_answer(store.verified_rows(dataset_id), case["task"], config)
        if canonical_hash(expected) != case["expected_answer_hash"]:
            raise FinalAssessmentError("Final oracle hash changed")
        if history.active_version(config.task_family).get("commit") != commit:
            raise FinalAssessmentError("Active commit changed during final assessment")
        claim = {"_id": case_id, "case_id": case_id, "dataset": {"id": dataset_id,
                 "content_hash": dataset.content_hash, "row_count": dataset.row_count},
                 "task_family": config.task_family, "commit": commit, "status": "running",
                 "reserved_at": document["reserved_at"], "started_at": utc_now()}
        try:
            history.final_assessments.insert_one(claim)
        except DuplicateKeyError as exc:
            raise FinalAssessmentError(f"Final case {case_id} has already been used") from exc
        try:
            execution = run(dataset, case["question"], case_id)
            result = execution.result
            still_active = (history.active_version(config.task_family) or {}).get("commit") == commit
            violation = (_resource_violation(result, config)
                         or ("active_commit_changed" if not still_active else None)
                         or ("incomplete_history" if execution.history_status != "recorded" else None)
                         or ("incomplete_trace" if config.evaluation.require_trace and execution.trace.status != "available" else None)
                         or (result.error if result.error else None)
                         or ("wrong_or_missing_answer" if result.outcome != "answered" or result.answer != expected else None))
            passed = violation is None
            evaluation = build_evaluation_record(evaluation_id=new_trial_id(), case_id=case_id,
                result=result, passed=passed, violation=violation, dataset=dataset,
                trace=execution.trace, config=config, created_at=utc_now())
            evaluation["role"] = "final_assessment"
            history.record_evaluation(evaluation)
            verdict = {"status": "completed", "run_id": result.run_id, "passed": passed,
                       "outcome": result.outcome, "violation": violation,
                       "actual": result.answer, "expected": expected,
                       "resources": {"model_calls": result.model_calls, "tool_calls": result.tool_calls,
                                     "total_tokens": result.total_tokens, "elapsed_seconds": result.elapsed_seconds,
                                     "table_pages": result.table_pages, "table_bytes": result.table_bytes},
                       "trace_id": execution.trace.trace_id, "history_status": execution.history_status,
                       "completed_at": utc_now()}
        except Exception as exc:
            verdict = {"status": "failed", "passed": False, "violation": type(exc).__name__, "completed_at": utc_now()}
            history.final_assessments.update_one({"_id": case_id}, {"$set": verdict})
            results.append({"case_id": case_id, "dataset_id": dataset_id, "commit": commit, **verdict})
            continue
        history.final_assessments.update_one({"_id": case_id}, {"$set": verdict})
        results.append({"case_id": case_id, "dataset_id": dataset_id, "commit": commit, **verdict})
    return results
