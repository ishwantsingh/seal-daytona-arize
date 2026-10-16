"""Durable workflow and evolution-job contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from self_heal.database import SQLiteDatabase

from self_heal.evolution_jobs import EvolutionJobService
from self_heal.logistics_evolution import FamilyEvolutionController
from self_heal.model import ModelReply
from self_heal.settings import PhoenixConfig, load_config
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore
from self_heal.telemetry import PhoenixTelemetry
from self_heal.web import WebApplication
from self_heal.workflow import extract_workflow, workflow_diff


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals" / "analyst" / "data" / "small_inventory.json"


class Model:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = iter(replies)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        return next(self.replies)


def _history() -> HistoryStore:
    history = HistoryStore(SQLiteDatabase())
    history.ensure_indexes()
    return history


def _application(replies: list[ModelReply]) -> WebApplication:
    config = load_config()
    history = _history()
    store = TableStore(history.runs.database, config)
    store.ensure_indexes()
    store.materialize("small", json.loads(FIXTURE.read_text())["rows"])
    tracing = PhoenixConfig(enabled=False, api_key=None, project="tests", base_url="http://localhost:6006")
    return WebApplication(store=store, history=history, config=config,
                          telemetry=PhoenixTelemetry(tracing), model_factory=lambda: Model(list(replies)),
                          tracing=tracing)


def test_workflow_snapshot_is_typed_deduplicated_and_diffable():
    history, config = _history(), load_config()
    snapshot = extract_workflow(source=ROOT, source_commit="base", config=config)
    stored = history.record_workflow(snapshot.record)
    assert history.record_workflow(snapshot.record)["workflow_revision_id"] == stored["workflow_revision_id"]
    nodes = {node["id"]: node for node in stored["graph"]["nodes"]}
    assert nodes["agent:inventory"]["kind"] == "agent"
    assert nodes["tool:read_rows"]["group"] == "agent:inventory"
    assert nodes["data:sqlite"]["kind"] == "data_source"
    assert stored["graph"]["metadata"]["runtime_mcp_servers"] == []

    candidate = extract_workflow(source=ROOT, source_commit="candidate", config=config)
    candidate_record = dict(candidate.record)
    candidate_record["graph"] = {**candidate_record["graph"], "nodes": [
        *candidate_record["graph"]["nodes"],
        {"id": "tool:proposed", "kind": "tool", "label": "proposed", "fingerprint": "changed"},
    ]}
    candidate_record["graph_hash"] = "candidate-graph"
    history.record_workflow(candidate_record)
    diff = workflow_diff(stored, candidate_record)
    assert [node["id"] for node in diff["nodes"]["added"]] == ["tool:proposed"]


def test_historical_logistics_workflow_does_not_invent_an_unregistered_tool(tmp_path):
    source = tmp_path / "historical"
    (source / "harness").mkdir(parents=True)
    (source / "harness" / "agent.py").write_text("# inventory-only baseline\n")
    config = load_config()
    snapshot = extract_workflow(
        source=source, source_commit="inventory-only", config=config,
        task_family="logistics-shipment-threshold",
    )
    nodes = {node["id"]: node for node in snapshot.graph["nodes"]}
    assert nodes["agent:logistics"]["label"] == "No logistics agent registered"
    assert "tool:count_customers_over_shipment_threshold" not in nodes
    assert snapshot.graph["metadata"]["missing_components"] == [
        "logistics agent", "logistics tools", "logistics data adapter",
    ]


def test_browser_run_binds_a_saved_workflow_and_queues_one_durable_job():
    app = _application([ModelReply('{"error":"Revenue is outside the inventory contract"}', (), 4)])
    _, run = app.api("POST", "/api/runs", {"dataset_id": "small", "question": "What is total revenue?"})
    assert run["workflow_revision_id"].startswith("workflow_")
    assert run["evolution_job_id"].startswith("evolution_")
    _, workflow = app.api("GET", "/api/harness-workflows/" + run["workflow_revision_id"])
    assert workflow["graph"]["nodes"]
    source = app.history.get_run(run["run_id"])["execution"]["source"]
    assert source["dirty"] is False
    assert source["commit"] == workflow["source_commit"]
    _, jobs = app.api("GET", "/api/evolution-jobs/" + run["evolution_job_id"] + "/events?after=0")
    assert [event["stage"] for event in jobs["events"]] == ["incident_classified", "workflow_snapshot_ready", "blocked"]
    _, repeated = app.evolution_jobs.queue(incident_run_id=run["run_id"], workflow_revision_id=run["workflow_revision_id"])
    assert repeated is False


def test_background_job_records_replayable_progress_without_a_browser():
    history = _history()
    history.runs.insert_one({"_id": "incident", "run_id": "incident", "status": "completed"})

    def controller_factory(progress):
        class Controller:
            def evolve(self, run_id):
                progress("proposal_received", {"candidate_id": "candidate", "hypothesis": "Add a bounded tool"})
                progress("evaluating", {"candidate_id": "candidate"})
                return {"run_id": run_id, "status": "open", "reason": "Selection rejected"}
        return Controller()

    service = EvolutionJobService(history=history, controller_factory=controller_factory)
    job, created = service.queue(incident_run_id="incident", workflow_revision_id="workflow_base", task_family="inventory-totals")
    assert created
    for _ in range(100):
        current = history.get_evolution_job(job["job_id"])
        if current and current.get("status") == "rejected":
            break
        time.sleep(0.01)
    current = history.get_evolution_job(job["job_id"])
    assert current and current["status"] == "rejected"
    events = history.evolution_events_after(job["job_id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["stage"] for event in events] == ["incident_classified", "workflow_snapshot_ready", "diagnosing", "proposal_received", "evaluating", "rejected"]


def test_background_job_dispatches_a_logistics_incident_to_its_controller():
    """A logistics gap must never fall through to the SQLite-only controller."""

    history = _history()
    history.runs.insert_one({
        "_id": "logistics-incident", "run_id": "logistics-incident", "status": "completed",
        "invocation": {"task_family": "logistics-shipment-threshold"},
    })
    selected: list[str] = []

    class LogisticsController:
        def evolve(self, run_id):
            selected.append(run_id)
            return {"run_id": run_id, "status": "open", "reason": "Selection rejected"}

    def controller_factory(_progress):
        return FamilyEvolutionController(
            history=history,
            inventory=lambda: (_ for _ in ()).throw(AssertionError("inventory controller selected")),
            logistics=LogisticsController,
        )

    service = EvolutionJobService(history=history, controller_factory=controller_factory)
    job, created = service.queue(
        incident_run_id="logistics-incident", workflow_revision_id="workflow_logistics",
        task_family="logistics-shipment-threshold",
    )
    assert created
    for _ in range(100):
        current = history.get_evolution_job(job["job_id"])
        if current and current.get("status") == "rejected":
            break
        time.sleep(0.01)
    current = history.get_evolution_job(job["job_id"])
    assert current and current["status"] == "rejected"
    assert selected == ["logistics-incident"]


def test_logistics_workflow_records_model_loop_and_preserves_historical_dispatch(tmp_path):
    current = extract_workflow(source=ROOT, source_commit="current", config=load_config(),
                               task_family="logistics-shipment-threshold")
    assert current.graph["metadata"]["execution_mode"] == "model_tool_loop"
    assert any(node["id"] == "external:model" for node in current.graph["nodes"])
    assert any(edge["source"] == "agent:logistics" and edge["target"] == "external:model"
               for edge in current.graph["edges"])

    source = tmp_path / "historical-dispatch"
    (source / "harness").mkdir(parents=True)
    (source / "harness" / "logistics.py").write_text('''
class LogisticsAgent:
    def run(self, invocation):
        return self.tools.execute("count_customers_over_shipment_threshold", invocation)
''')
    old = extract_workflow(source=source, source_commit="old", config=load_config(),
                           task_family="logistics-shipment-threshold")
    assert old.graph["metadata"]["execution_mode"] == "direct_tool_dispatch"
    assert all(node["id"] != "external:model" for node in old.graph["nodes"])


def test_changed_source_under_same_version_label_creates_a_new_immutable_workflow(tmp_path):
    import shutil

    source = tmp_path / "source"
    shutil.copytree(ROOT / "harness", source / "harness")
    history, config = _history(), load_config()
    for family, relative in [(config.task_family, "agent.py"),
                             ("logistics-shipment-threshold", "logistics.py")]:
        first = extract_workflow(source=source, source_commit="same-version", config=config, task_family=family)
        history.record_workflow(first.record)
        path = source / "harness" / relative
        path.write_text(path.read_text() + "\n# Updated local implementation\n")
        second = extract_workflow(source=source, source_commit="same-version", config=config, task_family=family)
        assert second.graph_hash != first.graph_hash
        assert second.revision_id != first.revision_id
        history.record_workflow(second.record)
        assert history.harness_workflows.find_one({"_id": first.revision_id})["graph_hash"] == first.graph_hash
        assert history.record_workflow(second.record)["workflow_revision_id"] == second.revision_id


def test_evolution_blocker_is_not_misreported_as_an_operational_error():
    from self_heal.controller import EvolutionBlocked
    history = _history()
    history.runs.insert_one({"_id": "blocked-incident", "run_id": "blocked-incident", "status": "completed"})
    class Controller:
        def evolve(self, _run_id):
            raise EvolutionBlocked("Pinned evidence is unavailable")
    service = EvolutionJobService(history=history, controller_factory=lambda _progress: Controller())
    job, _ = service.queue(incident_run_id="blocked-incident", workflow_revision_id=None)
    service._threads[job["job_id"]].join(timeout=5)
    stored = history.get_evolution_job(job["job_id"])
    assert stored["status"] == "blocked"
    assert stored["reason"] == "Pinned evidence is unavailable"
    assert history.evolution_events_after(job["job_id"])[-1]["stage"] == "blocked"
