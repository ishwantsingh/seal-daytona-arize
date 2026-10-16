import json
import uuid
from dataclasses import replace
from pathlib import Path

from self_heal.database import SQLiteDatabase
import pytest

from evals.logistics.generator import public_incident_bundle
from evals.logistics.oracle import reference_answer
from harness.agent import AnalystAgent, RunResult
from harness.logistics import LogisticsAgent, LogisticsTools, TOOL_NAME
from harness.tools import AnalystTools
from self_heal.input_router import InputRouter
from self_heal.logistics_store import LogisticsDatasetStore
from self_heal.logistics_evaluation import run_logistics_checks
from self_heal.logistics_evolution import FamilyEvolutionController, LogisticsEvolutionController
from self_heal.execution import RunExecution
from self_heal.model import ModelReply, ToolCall
from self_heal.repository import CandidateSource
from self_heal.telemetry import TraceEvidence
from self_heal.settings import PhoenixConfig, load_config
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore, DatasetError, TableAccessError
from self_heal.telemetry import PhoenixTelemetry


def materialized_store():
    database = SQLiteDatabase()
    store = LogisticsDatasetStore(database)
    store.ensure_indexes()
    bundle = public_incident_bundle()
    info = store.materialize("logistics-shipment-threshold-public-v1", **bundle)
    return store, database, info, bundle


def test_public_bundle_is_immutable_and_oracle_is_hand_checked():
    store, database, info, bundle = materialized_store()
    task = {"operation": "count_customers_with_shipment_count_gt", "warehouse_number": 3, "relative_day": "yesterday", "threshold": 15}
    assert info.relations["shipments"]["row_count"] == 74
    assert reference_answer(bundle, task) == {"value": 2}
    assert store.materialize(info.dataset_id, **bundle) == info
    changed = {**bundle, "shipments": bundle["shipments"][:-1]}
    with pytest.raises(DatasetError, match="cannot be changed"):
        store.materialize(info.dataset_id, **changed)
    database.logistics_shipments.update_one({"dataset_id": info.dataset_id, "position": 0}, {"$set": {"status": "cancelled"}})
    with pytest.raises(DatasetError, match="content changed"):
        store.dataset_info(info.dataset_id)


def test_bundle_validation_rejects_foreign_keys_timestamps_and_duplicates():
    public_incident_bundle()
    for mutation, message in [
        (lambda value: value["shipments"][0].update(sender_customer_id="missing"), "orphaned"),
        (lambda value: value["shipments"][0].update(sent_at="2026-09-27T00:00:00Z"), "later"),
        (lambda value: value["shipments"].append(dict(value["shipments"][0])), "Duplicate"),
        (lambda value: value.update(reporting_timezone="Not/AZone"), "timezone"),
    ]:
        candidate = public_incident_bundle()
        mutation(candidate)
        store = LogisticsDatasetStore(SQLiteDatabase())
        with pytest.raises(DatasetError, match=message):
            store.materialize("bad", **candidate)


def test_logistics_session_is_filter_bound_and_cursor_bound():
    store, _, info, _ = materialized_store()
    session = store.open_session(info.dataset_id)
    assert session.inspect_catalog()["relations"]["shipments"]["row_count"] == 74
    first = session.read_shipments(warehouse_number=3, relative_day="yesterday", limit=4)
    assert len(first["shipments"]) == 4
    assert first["next_cursor"]
    with pytest.raises(TableAccessError, match="cursor"):
        session.read_shipments(warehouse_number=2, relative_day="yesterday", limit=4, cursor=first["next_cursor"])
    with pytest.raises(TableAccessError, match="Invalid"):
        session.read_shipments(warehouse_number=3, relative_day="today", limit=4)


class NoCallsModel:
    def complete(self, *_args):
        raise AssertionError("Structured tool checks must not call a model")


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append((messages, tools))
        return next(self.replies)


def logistics_model(warehouse=3, threshold=15, value=2):
    return ScriptedModel([
        ModelReply(None, (ToolCall("threshold", TOOL_NAME, json.dumps({
            "warehouse_number": warehouse, "relative_day": "yesterday", "threshold": threshold,
        })),), 20),
        ModelReply(json.dumps({"value": value}), (), 10),
    ])


@pytest.mark.parametrize("question", [
    "how many customers sent more than 15 shipments from warehouse 3 yesterday",
    "Yesterday at warehouse three, count senders who shipped over fifteen times.",
    "Count clients with 16 or more sent shipments yesterday from depot #3.",
    "  How  many\tcustomers sent more than 15 shipments\nfrom warehouse 3 yesterday ?  ",
])
def test_logistics_questions_reach_the_model_with_registered_tools(question):
    store, _, info, _ = materialized_store()
    model = logistics_model()
    result = LogisticsAgent(model, LogisticsTools(store.open_session(info.dataset_id), load_config()), load_config()).run(question)
    assert result.outcome == "answered", result.error
    assert result.answer == {"value": 2}
    assert result.model_calls == 2 and result.tool_calls == 1
    assert question.strip() in model.seen[0][0][1]["content"]
    assert model.seen[0][1][0]["function"]["name"] == TOOL_NAME
    assert model.seen[1][0][-1]["role"] == "tool"
    assert result.interpreted_task["threshold"] == 15


def test_inventory_baseline_asks_model_to_identify_missing_capabilities():
    config = load_config()
    inventory = TableStore(SQLiteDatabase(), config)
    inventory.ensure_indexes()
    inventory.materialize("inventory", [{"sku": "A", "warehouse": "East", "category": "Hardware", "on_hand": 1, "reserved": 0}])
    table = inventory.open_session("inventory")
    model = ScriptedModel([ModelReply(json.dumps({"unsupported": {
        "reason": "Shipment data and aggregation are unavailable",
        "capability_request": {"operation": "count_customers_with_shipment_count_gt", "threshold": 15},
    }}), (), 20)])
    result = AnalystAgent(model, AnalystTools(table, config), config).run(
        "How many customers sent more than 15 shipments from warehouse 3 yesterday?"
    )
    assert result.outcome == "unsupported"
    assert result.limitation_kind == "capability_gap"
    assert result.capability_request["threshold"] == 15
    assert result.model_calls == 1
    assert result.tool_calls == result.table_pages == result.table_bytes == 0
    assert {tool["function"]["name"] for tool in model.seen[0][1]} == {"inspect_table", "read_rows", "calculate"}


def test_reviewed_logistics_tool_matches_oracle_across_thresholds_and_pages():
    store, _, info, bundle = materialized_store()
    config = load_config()
    for warehouse, threshold in ((3, 15), (3, 16), (3, 17), (2, 1)):
        session = store.open_session(info.dataset_id)
        tools = LogisticsTools(session, config)
        question = f"How many customers sent more than {threshold} shipments from warehouse {warehouse} yesterday?"
        task = {"operation": "count_customers_with_shipment_count_gt",
                "warehouse_number": warehouse, "relative_day": "yesterday", "threshold": threshold}
        result = LogisticsAgent(NoCallsModel(), tools, config).run(task)
        expected = reference_answer(bundle, {"operation": "count_customers_with_shipment_count_gt",
            "warehouse_number": warehouse, "relative_day": "yesterday", "threshold": threshold})
        assert result.outcome == "answered"
        assert result.answer == expected
        assert result.model_calls == 0 and result.tool_calls == 1
        assert session.pages_read >= 1
        assert tools.definitions()[0]["function"]["name"] == TOOL_NAME


def test_logistics_checks_record_four_oracle_graded_trials():
    database = SQLiteDatabase()
    store = LogisticsDatasetStore(database)
    store.ensure_indexes()
    history = HistoryStore(database)
    history.ensure_indexes()
    telemetry = PhoenixTelemetry(PhoenixConfig(enabled=False, project="test"))
    results = run_logistics_checks(store, history, load_config(), telemetry)
    assert len(results) == 4 and all(item["passed"] for item in results)
    assert [item["actual"]["value"] for item in results] == [2, 1, 0, 1]
    assert history.evaluations.count_documents({"task_family": "logistics-shipment-threshold"}) == 4
    assert all(item["role"] == "Logistics tool check" for item in history.evaluations.find({}))


def test_router_requires_an_explicit_input_kind():
    config = load_config()
    database = SQLiteDatabase()
    tables = TableStore(database, config)
    tables.ensure_indexes()
    tables.materialize("inventory", [{"sku": "A", "warehouse": "East", "category": "Hardware", "on_hand": 1, "reserved": 0}])
    logistics = LogisticsDatasetStore(database); logistics.ensure_indexes()
    logistics.materialize("logistics", **public_incident_bundle())
    router = InputRouter(tables, logistics)
    assert router.describe(input_kind="inventory_table", dataset_id="inventory").domain == "inventory"
    assert router.describe(input_kind="logistics_bundle", dataset_id="logistics").relation_counts == {"customers": 8, "warehouses": 3, "shipments": 74}
    with pytest.raises(ValueError, match="input kind"):
        router.describe(input_kind="unknown", dataset_id="inventory")


def test_logistics_evolution_uses_the_bundle_controller_and_records_paired_trials():
    store, database, dataset, _ = materialized_store()
    history = HistoryStore(database)
    history.ensure_indexes()
    config = load_config()
    question = "How many customers sent more than 15 shipments from warehouse 3 yesterday?"
    history.runs.insert_one({
        "_id": "logistics-gap", "run_id": "logistics-gap", "status": "completed",
        "outcome": "unsupported", "limitation_kind": "capability_gap",
        "limitation_reason": "A bounded shipment tool is required",
        "invocation": {"question": question, "task_family": "logistics-shipment-threshold"},
        "dataset": {"id": dataset.dataset_id, "content_hash": dataset.content_hash,
                    "input_kind": "logistics_bundle"},
        "execution": {"source": {"commit": "baseline", "dirty": False}},
        "trace": {"status": "disabled"},
    })
    inventory = TableStore(database, config)
    inventory.ensure_indexes()
    inventory.materialize("small", [{"sku": "A", "warehouse": "East", "category": "Tools", "on_hand": 1, "reserved": 0}])

    class Runner:
        def __init__(self):
            self.store, self.logistics = inventory, store

        def image_identity(self): return "sha256:test"

        def dataset_info(self, info):
            return (store.dataset_info(info.dataset_id) if getattr(info, "input_kind", "inventory_table") == "logistics_bundle"
                    else inventory.dataset_info(info.dataset_id))

        def run(self, *, source_commit, dataset, invocation, **_kwargs):
            # Test runner uses independently frozen task parameters, never production question parsing.
            import re
            match = re.search(r"more than (\d+) shipments from warehouse (\d+)", invocation) if isinstance(invocation, str) and "yesterday" in invocation else None
            request = {"operation": "count_customers_with_shipment_count_gt", "threshold": int(match[1]),
                       "warehouse_number": int(match[2]), "relative_day": "yesterday"} if match else None
            if getattr(dataset, "input_kind", "inventory_table") == "logistics_bundle" and request and source_commit == "candidate":
                task = {key: request[key] for key in ("operation", "warehouse_number", "relative_day", "threshold")}
                bundle = {**store.verified_relations(dataset.dataset_id),
                          "reference_instant": dataset.reference_instant.isoformat(),
                          "reporting_timezone": dataset.reporting_timezone}
                result = RunResult(str(uuid.uuid4()), reference_answer(bundle, task), None, "answered", task, 0, 1, 0, 0.01, 2, 100)
            elif getattr(dataset, "input_kind", "inventory_table") == "logistics_bundle":
                result = RunResult(str(uuid.uuid4()), None, None, "unsupported", None, 0, 0, 0, 0.01, 0, 0,
                                   limitation_kind="capability_gap", limitation_reason="Missing tool")
            else:
                result = RunResult(str(uuid.uuid4()), {"value": 0}, None, "answered", {}, 0, 0, 0, 0.01, 0, 0)
            return RunExecution(result, TraceEvidence(None, None, "disabled", "test", None, None), "recorded")

    class Repository:
        def resolve_commit(self, _commit): return "baseline"
        def create_worktree(self, _commit): return Path(__file__).resolve().parents[1]
        def apply_proposal(self, parent, diff):
            return CandidateSource(parent, "candidate", Path(__file__).resolve().parents[1], diff, ("harness/logistics.py",))
        def inspect(self, _source): pass

    class ProposalModel:
        def complete(self, messages, *_args):
            if "independent logistics oracle" in messages[0]["content"]:
                return ModelReply(json.dumps({"operation": "count_customers_with_shipment_count_gt",
                    "warehouse_number": 3, "relative_day": "yesterday", "threshold": 15}))
            return ModelReply(json.dumps({"hypothesis": "A bounded tool is missing", "changed_mechanism": "tool registration",
                "diff": "diff --git a/harness/logistics.py b/harness/logistics.py\n"}), (), 1)

    events = []
    controller = LogisticsEvolutionController(
        logistics=store, history=history, config=replace(
            config, task_family="logistics-shipment-threshold", task_contract_version="logistics-shipment-threshold-v1",
        ), telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
        repository=Repository(), runner=Runner(), model_factory=NoCallsModel,
        evolution_model=ProposalModel(), on_progress=lambda stage, payload: events.append(stage),
    )
    router = FamilyEvolutionController(history=history, inventory=lambda: pytest.fail("inventory controller selected"),
                                       logistics=lambda: controller)
    result = router.evolve("logistics-gap")
    assert result["status"] == "open"
    assert "candidate_ready" in events and "evaluating" in events
    assert history.candidates.count_documents({"incident_run_id": "logistics-gap"}) == 1
    assert history.selection_plans.count_documents({}) == 1
    assert history.evaluations.count_documents({"plan_id": {"$exists": True}}) > 0


def test_logistics_proposer_receives_the_observed_tool_boundary_and_page_schema():
    from self_heal.evolution import propose_logistics_change

    class CaptureModel:
        def complete(self, messages, tools):
            self.messages = messages
            return ModelReply(json.dumps({
                "hypothesis": "Add bounded aggregation",
                "changed_mechanism": "One observed execute call pages the scoped session",
                "diff": "--- a/harness/logistics.py\n+++ b/harness/logistics.py\n@@ -1 +1 @@\n-# missing\n+# implemented\n",
            }))

    model = CaptureModel()
    propose_logistics_change(model, incident={"outcome": "unsupported"}, trace=[],
                            contract={"input_kind": "logistics_bundle"},
                            source={"logistics.py": "# missing"}, reproduction={}, previous_attempts=[])
    interface = model.messages[0]["content"]
    assert '"sender_customer_id"' in interface
    assert '"next_cursor"' in interface
    assert "only `definitions()`, `execute(name, arguments)`, and `table`" in interface
    assert "custom tool helper methods\nare not forwarded" in interface
    assert "rather\nthan one tool call per page" in interface


def test_new_logistics_tool_is_available_without_changing_question_routing():
    class ExtendedTools(LogisticsTools):
        def definitions(self):
            return super().definitions() + [{"type": "function", "function": {
                "name": "shipment_row_count", "description": "Count all shipment records in the assigned bundle.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }}]

        def execute(self, name, arguments):
            if name == "shipment_row_count" and arguments == {}:
                return {"value": self.table.inspect_relation("shipments")["row_count"]}
            return super().execute(name, arguments)

    store, _, dataset, _ = materialized_store()
    config = load_config()
    model = ScriptedModel([
        ModelReply(None, (ToolCall("count", "shipment_row_count", "{}"),), 10),
        ModelReply('{"task":{"operation":"shipment_row_count"},"answer":{"value":74}}', (), 10),
    ])
    result = LogisticsAgent(model, ExtendedTools(store.open_session(dataset.dataset_id), config), config).run(
        "How many shipment records are stored?"
    )
    assert result.outcome == "answered", result.error
    assert result.answer == {"value": 74}
    assert result.interpreted_task == {"operation": "shipment_row_count"}
    assert {tool["function"]["name"] for tool in model.seen[0][1]} == {TOOL_NAME, "shipment_row_count"}


def test_logistics_evolution_preserves_new_operations_as_needing_an_oracle():
    store, database, dataset, _ = materialized_store()
    history = HistoryStore(database)
    history.ensure_indexes()
    question = "Show monthly shipping totals by customer segment."
    history.runs.insert_one({
        "_id": "new-operation", "run_id": "new-operation", "status": "completed",
        "outcome": "unsupported", "limitation_kind": "capability_gap",
        "invocation": {"question": question, "task_family": "logistics-shipment-threshold"},
        "capability_request": {"operation": "monthly_shipments_by_segment"},
        "dataset": {"id": dataset.dataset_id, "content_hash": dataset.content_hash, "input_kind": "logistics_bundle"},
    })
    model = ScriptedModel([ModelReply('{"needs_contract":true}')])
    controller = LogisticsEvolutionController(
        logistics=store, history=history, config=load_config(),
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
        repository=None, runner=None, model_factory=NoCallsModel, evolution_model=model,
    )
    result = controller.evolve("new-operation")
    assert result["status"] == "needs_contract"
    assert result["proposal"]["operation"] == "monthly_shipments_by_segment"
    assert history.get_gap("new-operation")["status"] == "needs_contract"
    assert question in model.seen[0][0][1]["content"]


def test_model_can_combine_registered_tool_results_for_a_different_request():
    store, _, dataset, _ = materialized_store()
    config = load_config()
    model = ScriptedModel([
        ModelReply(None, (ToolCall("warehouse-three", TOOL_NAME, json.dumps({
            "warehouse_number": 3, "relative_day": "yesterday", "threshold": 15,
        })),), 10),
        ModelReply(None, (ToolCall("warehouse-two", TOOL_NAME, json.dumps({
            "warehouse_number": 2, "relative_day": "yesterday", "threshold": 1,
        })),), 10),
        ModelReply('{"task":{"operation":"difference_of_customer_counts"},"answer":{"value":1}}', (), 10),
    ])
    result = LogisticsAgent(model, LogisticsTools(store.open_session(dataset.dataset_id), config), config).run(
        "Yesterday, how many more customers sent over 15 shipments from warehouse 3 than sent over 1 from warehouse 2?"
    )
    assert result.outcome == "answered", result.error
    assert result.answer == {"value": 1}
    assert result.interpreted_task == {"operation": "difference_of_customer_counts"}
    assert result.model_calls == 3 and result.tool_calls == 2
    assert json.loads(model.seen[2][0][-1]["content"])["value"] == 1
