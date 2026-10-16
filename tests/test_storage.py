import json
from pathlib import Path

from self_heal.database import SQLiteDatabase
import pytest

from harness.agent import RunResult
from self_heal.contracts import (
    build_eval_case_record,
    build_evaluation_record,
    build_run_completion_patch,
    build_run_start_record,
    new_candidate_id,
    new_evaluation_id,
    new_version_id,
    stable_case_id,
    utc_now,
)
from self_heal.settings import load_config
from self_heal.storage import HistoryStore, HistoryError
from self_heal.table_store import TableStore
from self_heal.telemetry import TraceEvidence


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "data" / "small_inventory.json"


class StubModel:
    model = "scripted-test"


def make_history():
    config = load_config()
    database = SQLiteDatabase()
    tables = TableStore(database, config)
    tables.ensure_indexes()
    dataset = tables.materialize("small", json.loads(FIXTURE.read_text())["rows"])
    history = HistoryStore(database)
    history.ensure_indexes()
    return history, database, config, dataset


def complete_run(history, config, dataset, *, run_id, invocation, result, trace_id=None):
    history.start_run(
        build_run_start_record(
            run_id=run_id,
            invocation=invocation,
            dataset=dataset,
            config=config,
            model=StubModel(),
            started_at=utc_now(),
        )
    )
    history.finish_run(
        run_id,
        build_run_completion_patch(
            result=result,
            trace=TraceEvidence(
                trace_id=trace_id,
                url=f"https://smith.test/{trace_id}" if trace_id else None,
                status="available" if trace_id else "disabled",
                project="self-heal",
                started_at=None,
                checked_at=None,
            ),
            completed_at=utc_now(),
        ),
    )


def result_for(run_id, *, outcome="answered", answer=None, limitation_kind=None):
    return RunResult(
        run_id=run_id,
        answer=answer,
        error="provider failed" if outcome == "error" else None,
        outcome=outcome,
        interpreted_task={"metric": "available"} if outcome == "answered" else None,
        model_calls=1,
        tool_calls=0,
        total_tokens=10,
        elapsed_seconds=0.1,
        table_pages=0,
        table_bytes=0,
        limitation_kind=limitation_kind,
        limitation_reason="Question is ambiguous or unsupported" if limitation_kind else None,
    )


def test_history_links_immutable_run_to_dataset_and_trace_without_copying_rows():
    history, database, config, dataset = make_history()
    complete_run(
        history,
        config,
        dataset,
        run_id="run-1",
        invocation="How many available units are in East?",
        result=result_for("run-1", answer={"value": 18}),
        trace_id="run-1",
    )
    record = history.get_run("run-1")
    assert record is not None
    assert record["dataset"] == {
        "id": dataset.dataset_id,
        "content_hash": dataset.content_hash,
        "row_count": dataset.row_count,
    }
    assert record["trace"]["root_id"] == "run-1"
    assert record["invocation"]["task_id"].startswith("task_")
    assert record["execution"]["config"]["sha256"] == config.config_hash
    assert record["lifecycle"][0]["state"] == "started"
    assert record["lifecycle"][1]["state"] == "completed"
    assert "A-100" not in repr(record)
    assert "events" not in database.list_collection_names()
    duplicate = dict(record)
    duplicate["status"] = "running"
    with pytest.raises(HistoryError, match="Duplicate immutable run"):
        history.start_run(duplicate)


def test_capability_gap_and_future_history_records_are_queryable_without_conflating_errors():
    history, _, config, dataset = make_history()
    complete_run(
        history,
        config,
        dataset,
        run_id="gap",
        invocation="What is total revenue?",
        result=result_for("gap", outcome="unsupported", limitation_kind="capability_gap"),
        trace_id="gap",
    )
    complete_run(
        history,
        config,
        dataset,
        run_id="zero",
        invocation={"metric": "available", "filter_field": "sku", "filter_value": "missing"},
        result=result_for("zero", answer={"value": 0}),
    )
    complete_run(
        history,
        config,
        dataset,
        run_id="error",
        invocation="Question that made the provider fail",
        result=result_for("error", outcome="error"),
    )
    assert [record["run_id"] for record in history.capability_gaps(task_family=config.task_family)] == ["gap"]

    case_id = stable_case_id(
        scenario_id="small-east",
        dataset=dataset,
        task={"metric": "available"},
        oracle_version=config.evaluation.oracle_version,
    )
    case_record = build_eval_case_record(
        case_id=case_id,
        scenario_id="small-east",
        dataset=dataset,
        task={"metric": "available"},
        expected_answer={"value": 18},
        oracle_version=config.evaluation.oracle_version,
        config=config,
        exposure_role="baseline",
        created_at=utc_now(),
    )
    history.record_eval_case(case_record)
    history.record_eval_case(
        build_eval_case_record(
            case_id=case_id,
            scenario_id="small-east",
            dataset=dataset,
            task={"metric": "available"},
            expected_answer={"value": 18},
            oracle_version=config.evaluation.oracle_version,
            config=config,
            exposure_role="regression",
            created_at=utc_now(),
        )
    )
    assert len(history.eval_cases.find_one({"_id": case_id})["exposures"]) == 2

    candidate_id = new_candidate_id()
    history.record_candidate(
        {
            "_id": candidate_id,
            "candidate_id": candidate_id,
            "candidate_commit": "candidate-commit",
            "parent_commit": "parent-commit",
            "task_family": config.task_family,
            "changed_mechanism": "add-aggregate-tool",
            "hypothesis": "A reusable aggregate tool can answer the gap.",
            "created_at": utc_now(),
        }
    )
    assert [candidate["candidate_id"] for candidate in history.candidates_for(
        task_family=config.task_family, changed_mechanism="add-aggregate-tool"
    )] == [candidate_id]
    history.append_candidate_transition(candidate_id, status="rejected", at=utc_now(), reason="baseline regression")
    candidate = history.candidates.find_one({"_id": candidate_id})
    assert candidate["status"] == "rejected"
    assert [entry["state"] for entry in candidate["lifecycle"]] == ["proposed", "rejected"]

    history.record_evaluation(
        build_evaluation_record(
            evaluation_id=new_evaluation_id(),
            case_id=case_id,
            result=result_for("gap", outcome="unsupported", limitation_kind="capability_gap"),
            passed=False,
            violation="capability_refusal_for_answerable_case",
            dataset=dataset,
            trace=None,
            config=config,
            created_at=utc_now(),
        )
    )
    version_id = new_version_id()
    history.record_version(
        {
            "_id": version_id,
            "version_id": version_id,
            "identity_hash": "version-hash",
            "status": "candidate",
            "created_at": utc_now(),
        }
    )
    assert history.evaluations.count_documents({"case_id": case_id}) == 1
    assert history.versions.count_documents({"_id": version_id}) == 1
