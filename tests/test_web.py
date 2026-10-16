"""Browser-facing contract tests for the local Phase 3 operator interface."""

from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from self_heal.database import SQLiteDatabase
import pytest

from self_heal.model import ModelReply, ToolCall
from self_heal.settings import PhoenixConfig, load_config
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore
from self_heal.telemetry import PhoenixTelemetry
from self_heal.web import WebApplication, WebRequestError, create_server
from self_heal.execution import RunExecution
from harness.agent import RunResult
from self_heal.telemetry import TraceEvidence
from self_heal.contracts import utc_now
from self_heal.logistics_store import LogisticsDatasetStore


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "data" / "small_inventory.json"


class ScriptedModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = iter(replies)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        return next(self.replies)


def supported_model() -> ScriptedModel:
    return ScriptedModel(
        [
            ModelReply('{"metric":"available","filter_field":"warehouse","filter_value":"East"}', (), 10),
            ModelReply(
                None,
                (ToolCall("read-east", "read_rows", '{"limit":4,"filter_field":"warehouse","filter_value":"East"}'),),
                10,
            ),
            ModelReply('{"value":18}', (), 10),
        ]
    )


def unsupported_model() -> ScriptedModel:
    return ScriptedModel([ModelReply('{"error":"Revenue is outside the inventory contract"}', (), 10)])


def make_application(model_factory, *, dataset_ids: tuple[str, ...] = ("small",)):
    config = load_config()
    database = SQLiteDatabase()
    store = TableStore(database, config)
    store.ensure_indexes()
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    for dataset_id in dataset_ids:
        store.materialize(dataset_id, rows)
    history = HistoryStore(database)
    history.ensure_indexes()
    tracing = PhoenixConfig(enabled=False, api_key=None, project="self-heal-test", base_url="http://localhost:6006")
    return WebApplication(
        store=store,
        history=history,
        config=config,
        telemetry=PhoenixTelemetry(tracing),
        model_factory=model_factory,
        tracing=tracing,
    )


@pytest.fixture
def local_server():
    server = create_server(make_application(supported_model), port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def request_json(base_url: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None):
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        base_url + path,
        data=encoded,
        method=method,
        headers={"Content-Type": "application/json"} if encoded is not None else {},
    )
    with urlopen(request, timeout=2) as response:  # noqa: S310 -- loopback test server
        return response.status, json.loads(response.read())


def test_local_ui_serves_assets_and_only_dataset_metadata(local_server):
    with urlopen(local_server + "/", timeout=2) as response:  # noqa: S310 -- loopback test server
        page = response.read().decode("utf-8")
    assert "Ask about your data" in page
    assert "v1 → v2" not in page
    assert 'id="dataset"' not in page
    assert 'id="suggestions"' in page
    assert re.search(r'<textarea\b[^>]*\bid="question"', page)
    assert "Current workspace" not in page
    assert "Connected services" not in page
    assert 'class="navigation-bar"' in page
    assert page.index('id="analysis-form"') < page.index('id="run-details"') < page.index('data-history-list')
    assert 'hidden' in page.split('id="run-details"', 1)[1].split('>', 1)[0]

    status, datasets = request_json(local_server, "/api/datasets")
    assert status == 200
    assert datasets["datasets"] == [{"id": "small", "row_count": 6, "content_hash": datasets["datasets"][0]["content_hash"],
                                     "input_kind": "inventory_table", "domain": "inventory"}]
    assert "A-100" not in repr(datasets)

    status, preview = request_json(local_server, "/api/datasets/preview?dataset_id=small&input_kind=inventory_table")
    assert status == 200
    assert preview["relation"] == "analyst_rows"
    assert preview["row_count"] == 6
    assert preview["rows"][0]["sku"] == "A-100"
    assert preview["columns"] == ["sku", "warehouse", "category", "on_hand", "reserved"]

    with pytest.raises(HTTPError) as error:
        urlopen(local_server + "/%2e%2e/pyproject.toml", timeout=2)  # noqa: S310 -- loopback test server
    assert error.value.code == 404

    with urlopen(local_server + "/fonts/Geist.woff2", timeout=2) as response:
        assert response.headers["Content-Type"] == "font/woff2"
        assert response.read(4) == b"wOF2"
    with pytest.raises(HTTPError) as error:
        urlopen(local_server + "/fonts/%2e%2e/%2e%2e/pyproject.toml", timeout=2)
    assert error.value.code == 404


def test_local_ui_runs_the_phase_three_executor_and_exposes_compact_evidence(local_server):
    status, run = request_json(
        local_server,
        "/api/runs",
        method="POST",
        body={"dataset_id": "small", "question": "How many available units are in the East warehouse?"},
    )
    assert status == 201
    assert run["outcome"] == "answered"
    assert run["answer"] == {"value": 18}
    assert run["dataset"] == {"id": "small", "row_count": 6, "input_kind": "inventory_table", "domain": "inventory", "relations": None}
    assert run["resources"]["table_pages"] == 1
    assert run["history"]["status"] == "recorded"
    assert run["trace"]["status"] == "disabled"
    assert run["evidence"]["sqlite"]["row_count"] == 3
    assert run["evidence"]["sqlite"]["pages"][0]["rows"][0]["sku"] == "A-100"
    assert run["evidence"]["tool_calls"][0]["name"] == "read_rows"
    assert run["evidence"]["tool_calls"][0]["arguments"]["filter_value"] == "East"

    _, history = request_json(local_server, "/api/runs?limit=20")
    assert [entry["run_id"] for entry in history["runs"]] == [run["run_id"]]
    _, stored = request_json(local_server, "/api/runs/" + run["run_id"])
    assert stored["answer"] == {"value": 18}
    assert stored["trace"]["status"] == "disabled"
    assert stored["evidence"] == run["evidence"]


def test_local_ui_automatically_selects_an_operator_dataset():
    application = make_application(
        supported_model,
        dataset_ids=("eval-bulk-warehouse-available-v1", "incident-generated", "private-generated", "small"),
    )
    server = create_server(application, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        status, run = request_json(
            f"http://{host}:{port}",
            "/api/runs",
            method="POST",
            body={"question": "How many available units are in the East warehouse?"},
        )
        assert status == 201
        assert run["answer"] == {"value": 18}
        assert run["dataset"] == {"id": "small", "row_count": 6, "input_kind": "inventory_table", "domain": "inventory", "relations": None}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_logistics_ui_loads_public_bundle_and_records_reviewed_tool_call():
    app = make_application(supported_model)
    app.logistics = LogisticsDatasetStore(app.history.runs.database)
    app.logistics.ensure_indexes()
    status, seeded = app.api("POST", "/api/datasets/logistics", {})
    assert status == 201
    assert seeded["relations"] == {"customers": 8, "warehouses": 3, "shipments": 74}
    _, sources = app.api("GET", "/api/datasets")
    assert any(item["input_kind"] == "logistics_bundle" for item in sources["datasets"])
    _, preview = app.api("GET", f"/api/datasets/preview?dataset_id={seeded['id']}&input_kind=logistics_bundle")
    assert preview["relation"] == "shipments"
    assert preview["row_count"] == 74
    assert len(preview["rows"]) == 20
    _, customers = app.api("GET", f"/api/datasets/preview?dataset_id={seeded['id']}&input_kind=logistics_bundle&relation=customers")
    assert customers["row_count"] == 8
    assert customers["columns"] == ["customer_id", "segment", "status"]
    _, run = app.api("POST", "/api/runs", {"input_kind": "logistics_bundle", "dataset_id": seeded["id"],
        "question": "How many customers sent more than 15 shipments from warehouse 3 yesterday?"})
    assert run["outcome"] == "answered"
    assert run["answer"] == {"value": 2}
    assert run["capability_request"]["kind"] == "shipment_customer_threshold"
    assert run["resources"]["model_calls"] == 0
    assert run["resources"]["tool_calls"] == 1
    assert run["resources"]["table_pages"] > 0
    assert run["dataset"]["relations"]["shipments"] == 74
    _, stored = app.api("GET", "/api/runs/" + run["run_id"])
    assert stored["dataset"]["input_kind"] == "logistics_bundle"
    assert stored["dataset"]["relations"]["customers"]["row_count"] == 8
    assert stored["evidence"]["tool_calls"][0]["name"] == "count_customers_over_shipment_threshold"
    assert stored["evidence"]["tool_calls"][0]["result"]["value"] == 2
    assert app.history.get_run(run["run_id"])["invocation"]["task_family"] == "logistics-shipment-threshold"


def test_logistics_capability_gap_queues_an_evolution_job(monkeypatch):
    app = make_application(supported_model)
    app.logistics = LogisticsDatasetStore(app.history.runs.database)
    app.logistics.ensure_indexes()
    _, seeded = app.api("POST", "/api/datasets/logistics", {})

    def missing_capability(self, invocation, *, run_id=None):
        return RunResult(
            run_id or "gap", None, None, "unsupported", None, 0, 0, 0, 0, 0, 0,
            limitation_kind="capability_gap", limitation_reason="A shipment threshold tool is required",
            capability_request={"kind": "shipment_customer_threshold", "domain": "logistics"},
        )

    monkeypatch.setattr("self_heal.web.LogisticsAgent.run", missing_capability)
    status, run = app.api("POST", "/api/runs", {
        "input_kind": "logistics_bundle", "dataset_id": seeded["id"],
        "question": "How many customers sent more than 15 shipments from warehouse 3 yesterday?",
    })
    assert status == 201
    assert run["outcome"] == "unsupported"
    assert run["evolution_job_id"].startswith("evolution_")
    assert run["evolution_url"] == "#evolve/" + run["evolution_job_id"]
    stored = app.history.get_run(run["run_id"])
    assert stored and stored["evolution_job_id"] == run["evolution_job_id"]


def test_logistics_ui_uses_the_active_pinned_candidate(monkeypatch):
    app = make_application(supported_model)
    app.logistics = LogisticsDatasetStore(app.history.runs.database)
    app.logistics.ensure_indexes()
    _, seeded = app.api("POST", "/api/datasets/logistics", {})
    app.history.active_versions.insert_one({
        "_id": "logistics-shipment-threshold", "commit": "accepted-logistics",
        "workflow_revision_id": "workflow_logistics_candidate",
    })
    observed = {}

    def active_checkout(self, commit):
        observed["commit"] = commit
        return Path("/accepted-logistics")

    def candidate_run(self, **kwargs):
        observed.update({
            "source": kwargs["source"], "source_commit": kwargs["source_commit"],
            "dataset": kwargs["dataset"].dataset_id, "family": self.config.task_family,
        })
        return RunExecution(
            RunResult("logistics-candidate", {"value": 2}, None, "answered", {
                "operation": "count_customers_with_shipment_count_gt", "warehouse_number": 3,
                "relative_day": "yesterday", "threshold": 15,
            }, 0, 1, 0, 0.1, 2, 200),
            TraceEvidence(None, None, "disabled", "test", None, None), "recorded",
        )

    monkeypatch.setattr("self_heal.web.CandidateRepository.active_checkout", active_checkout)
    monkeypatch.setattr("self_heal.web.CandidateRunner.run", candidate_run)
    status, run = app.api("POST", "/api/runs", {
        "input_kind": "logistics_bundle", "dataset_id": seeded["id"],
        "question": "How many customers sent more than 15 shipments from warehouse 3 yesterday?",
    })
    assert status == 201
    assert run["answer"] == {"value": 2}
    assert observed == {
        "commit": "accepted-logistics", "source": Path("/accepted-logistics"),
        "source_commit": "accepted-logistics", "dataset": seeded["id"],
        "family": "logistics-shipment-threshold",
    }


def test_local_ui_never_auto_selects_a_protected_evaluation_table():
    application = make_application(supported_model, dataset_ids=("eval-only", "incident-generated", "private-generated"))
    with pytest.raises(WebRequestError, match="No operator dataset"):
        application.api("POST", "/api/runs", {"question": "How many available units are in the East warehouse?"})


def test_local_ui_records_unsupported_questions_as_capability_gaps():
    server = create_server(make_application(unsupported_model), port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    try:
        _, run = request_json(
            base_url,
            "/api/runs",
            method="POST",
            body={"dataset_id": "small", "question": "What is total revenue?"},
        )
        assert run["outcome"] == "unsupported"
        assert run["limitation_kind"] == "capability_gap"
        assert run["evidence"]["sqlite"]["row_count"] == 0
        assert run["evidence"]["tool_calls"] == []
        _, stored = request_json(base_url, "/api/runs/" + run["run_id"])
        assert stored["gap"]["candidates"] == []
        _, gaps = request_json(base_url, "/api/capability-gaps")
        assert [entry["run_id"] for entry in gaps["runs"]] == [run["run_id"]]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_rejected_candidate_is_never_reported_as_promoted():
    app = make_application(unsupported_model)
    _, run = app.api("POST", "/api/runs", {"question": "What is total revenue?"})
    run_id = run["run_id"]
    app.history.gaps.insert_one({"_id": run_id, "run_id": run_id, "status": "open", "created_at": run["created_at"]})
    app.history.eval_cases.insert_one({"_id": "case-1", "case_id": "case-1",
        "scenario_id": "observed-" + run_id.replace("-", "")[:24] + "-original"})
    app.history.candidates.insert_one({
        "_id": "candidate-1", "candidate_id": "candidate-1", "incident_run_id": run_id,
        "status": "rejected", "diff": "--- a/harness/agent.py\n+++ b/harness/agent.py",
        "created_at": run["created_at"],
        "selection": {"accepted": False, "reasons": ["original_failure_persists", "regression_failed"],
            "trials": [{"version": "candidate", "role": "original", "passed": False},
                       {"version": "candidate", "role": "regression", "passed": False}]},
    })
    _, detail = app.api("GET", "/api/runs/" + run_id)
    candidate = detail["gap"]["candidates"][0]
    assert candidate["status"] == "Rejected"
    assert candidate["correctness"] == "failed"
    assert candidate["regression"] == "failed"
    assert detail["gap"]["active_version"] == app.config.task_contract_version
    assert candidate["diff_url"]
    assert detail["gap"]["cases"][0]["url"]
    _, diff = app.api("GET", candidate["diff_url"])
    assert diff["candidate_id"] == "candidate-1"


def test_no_tool_run_reports_explicit_zero_evidence():
    app = make_application(unsupported_model)
    _, run = app.api("POST", "/api/runs", {"question": "What is total revenue?"})
    _, detail = app.api("GET", "/api/runs/" + run["run_id"])
    assert detail["outcome"] == "unsupported"
    assert detail["resources"]["tool_calls"] == 0
    assert detail["resources"]["table_pages"] == 0
    assert detail["evidence"]["tool_calls"] == []
    assert detail["evidence"]["sqlite"]["pages"] == []
    assert detail["evidence"]["sqlite"]["row_count"] == 0


def test_evaluation_and_version_pages_use_stored_records_without_exposing_final_data():
    app = make_application(supported_model, dataset_ids=("small", "final-secret"))
    _, datasets = app.api("GET", "/api/datasets")
    assert [item["id"] for item in datasets["datasets"]] == ["small"]
    with pytest.raises(WebRequestError, match="unavailable"):
        app.api("GET", "/api/datasets/preview?dataset_id=final-secret&input_kind=inventory_table")
    with pytest.raises(WebRequestError, match="Protected evaluation"):
        app.api("POST", "/api/runs", {"dataset_id": "final-secret", "question": "What is available?"})
    app.history.final_assessments.insert_one({"_id": "final-1", "case_id": "final-1",
        "status": "completed", "passed": False, "violation": "wrong_or_missing_answer",
        "run_id": "run-1", "commit": "abc", "started_at": utc_now(), "completed_at": utc_now()})
    _, evaluations = app.api("GET", "/api/evaluations")
    assert evaluations["evaluations"][0]["role"] == "Final assessment"
    assert evaluations["evaluations"][0]["passed"] is False
    _, versions = app.api("GET", "/api/versions")
    assert versions["active_commit"] is None
    assert versions["base_version"] == app.config.task_contract_version


def test_new_ui_task_uses_the_active_pinned_version(monkeypatch):
    application = make_application(supported_model)
    application.history.active_versions.insert_one({
        "_id": application.config.task_family, "commit": "accepted-commit",
    })
    observed = {}

    def active_checkout(self, commit):
        observed["commit"] = commit
        return Path("/accepted")

    def candidate_run(self, **kwargs):
        observed["source"] = kwargs["source"]
        observed["source_commit"] = kwargs["source_commit"]
        return RunExecution(
            RunResult("fresh", {"value": 18}, None, "answered",
                      {"metric": "available", "filter_field": "warehouse", "filter_value": "East"},
                      2, 1, 30, 0.1, 1, 100),
            TraceEvidence(None, None, "disabled", "test", None, None),
            "recorded",
        )

    monkeypatch.setattr("self_heal.web.CandidateRepository.active_checkout", active_checkout)
    monkeypatch.setattr("self_heal.web.CandidateRunner.run", candidate_run)
    status, payload = application.api("POST", "/api/runs", {
        "question": "How many available units are in the East warehouse?",
    })
    assert status == 201
    assert payload["answer"] == {"value": 18}
    assert payload["dataset"]["id"] == "small"
    assert observed == {"commit": "accepted-commit", "source": Path("/accepted"),
                        "source_commit": "accepted-commit"}


def test_local_ui_rejects_malformed_run_requests(local_server):
    request = Request(
        local_server + "/api/runs",
        data=b'{"dataset_id":"small"}',
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)  # noqa: S310 -- loopback test server
    assert error.value.code == 400
    assert json.loads(error.value.read())["error"] == "A run needs question and an optional dataset_id"


def test_running_job_exposes_persisted_plan_and_partial_trials_before_selection():
    app = make_application(supported_model)
    app.history.candidates.insert_one({"_id": "candidate-running"})
    app.history.selection_plans.insert_one({
        "_id": "plan-running", "candidate_id": "candidate-running",
        "cases": [{"case_id": "original", "role": "original"},
                  {"case_id": "private-secret", "role": "private_validation"}],
    })
    app.history.evaluations.insert_one({
        "_id": "trial-running", "trial_id": "trial-running", "plan_id": "plan-running",
        "case_id": "private-secret", "case_role": "private_validation", "passed": False,
        "violation": "private diagnostic", "candidate": {"version": "candidate"},
        "created_at": utc_now(), "resources": {"elapsed_seconds": 0.3},
    })
    result = app._job_evaluations({"candidate_id": "candidate-running", "status": "running"}, limit=100)
    assert result["plan_id"] == "plan-running"
    assert result["watermark"] == 1
    assert result["scheduled"]["candidate"] == {"original": 2, "private_validation": 2}
    assert result["groups"][0]["failed"] == 1
    assert result["groups"][0]["pending"] == 1
    assert result["trials"][0]["case_id"] is None
    assert result["trials"][0]["violation"] is None
    assert app._job_evaluations({"candidate_id": "not-started"}, limit=100)["plan_id"] is None


def test_logistics_run_version_retains_the_exact_executed_commit():
    from self_heal.web import _history_summary
    from harness.logistics import TOOL_VERSION

    record = {
        "run_id": "accepted-rerun", "outcome": "answered", "answer": {"value": 2},
        "dataset": {"input_kind": "logistics_bundle"},
        "interpreted_task": {"operation": "count_customers_with_shipment_count_gt",
                             "warehouse_number": 3, "relative_day": "yesterday", "threshold": 15},
        "execution": {"source": {"commit": "exact-tested-candidate"}},
    }
    assert _history_summary(record)["version"] == "exact-tested-candidate"
    assert _history_summary(record, detail=True)["version"] == "exact-tested-candidate"
    assert _history_summary({**record, "execution": {}})["version"] == TOOL_VERSION
