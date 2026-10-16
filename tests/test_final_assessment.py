"""Phase 6 one-use final assessment controls."""

import json
from pathlib import Path

from self_heal.database import SQLiteDatabase
import pytest

from evals.analyst.oracle import reference_answer
from harness.agent import RunResult
from self_heal.execution import RunExecution
from self_heal.final_assessment import FinalAssessmentError, assess_cases, reserve_cases
from self_heal.settings import load_config
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore
from self_heal.telemetry import TraceEvidence


def setup():
    config = load_config()
    database = SQLiteDatabase()
    store = TableStore(database, config)
    store.ensure_indexes()
    history = HistoryStore(database)
    history.ensure_indexes()
    return store, history, config


def test_final_reserve_assess_and_prevent_reuse(tmp_path):
    store, history, config = setup()
    manifest = tmp_path / "reserve.json"
    result = reserve_cases(store, config, manifest=manifest, checkout=Path.cwd())
    assert result["case_count"] == 2
    assert manifest.stat().st_mode & 0o777 == 0o600
    cases = json.loads(manifest.read_text())["cases"]
    assert cases[0]["dataset_id"].startswith("final-")
    history.active_versions.insert_one({"_id": config.task_family, "commit": "accepted"})

    def run(dataset, question, case_id):
        case = next(item for item in cases if item["case_id"] == case_id)
        answer = reference_answer(store.verified_rows(dataset.dataset_id), case["task"], config)
        if dataset.row_count == 512:
            answer = {"value": -1}
        result = RunResult(case_id, answer, None, "answered", case["task"], 2, 1, 40, 0.1, 1, 100)
        return RunExecution(result, TraceEvidence(case_id, None, "available", "test", None, None), "recorded")

    outcomes = assess_cases(store, history, config, manifest=manifest, checkout=Path.cwd(), run=run)
    assert [item["passed"] for item in outcomes] == [True, False]
    assert history.final_assessments.count_documents({}) == 2
    with pytest.raises(FinalAssessmentError, match="already been used"):
        assess_cases(store, history, config, manifest=manifest, checkout=Path.cwd(), run=run)


def test_final_manifest_and_exposure_are_protected(tmp_path):
    store, history, config = setup()
    with pytest.raises(FinalAssessmentError, match="outside"):
        reserve_cases(store, config, manifest=Path.cwd() / "private.json", checkout=Path.cwd())
    manifest = tmp_path / "reserve.json"
    reserve_cases(store, config, manifest=manifest, checkout=Path.cwd())
    case = json.loads(manifest.read_text())["cases"][0]
    history.active_versions.insert_one({"_id": config.task_family, "commit": "accepted"})
    history.eval_cases.insert_one({"_id": "exposed", "dataset": {"id": case["dataset_id"]}})
    with pytest.raises(FinalAssessmentError, match="exposed"):
        assess_cases(store, history, config, manifest=manifest, checkout=Path.cwd(), run=lambda *_: None)
    assert history.final_assessments.count_documents({}) == 0


def test_final_assessment_enforces_trace_and_resource_gates(tmp_path):
    store, history, config = setup()
    manifest = tmp_path / "reserve.json"
    reserve_cases(store, config, manifest=manifest, checkout=Path.cwd())
    cases = json.loads(manifest.read_text())["cases"]
    history.active_versions.insert_one({"_id": config.task_family, "commit": "accepted"})

    def run(dataset, question, case_id):
        case = next(item for item in cases if item["case_id"] == case_id)
        answer = reference_answer(store.verified_rows(dataset.dataset_id), case["task"], config)
        calls = 9 if dataset.row_count == 512 else 2
        result = RunResult(case_id, answer, None, "answered", case["task"], calls, 1, 40, 0.1, 1, 100)
        return RunExecution(result, TraceEvidence(None, None, "disabled", "test", None, None), "recorded")

    outcomes = assess_cases(store, history, config, manifest=manifest, checkout=Path.cwd(), run=run)
    assert outcomes[0]["violation"] == "incomplete_trace"
    assert outcomes[1]["violation"] == "model_call_limit_exceeded"
