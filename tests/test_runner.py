import json
import subprocess
import shutil
import tempfile
from pathlib import Path


from self_heal.database import SQLiteDatabase

from harness.tools import AnalystTools
from harness.logistics import LogisticsTools
from self_heal.model import ModelReply, ToolCall
from self_heal.runner import CandidateRunner
from self_heal.settings import PhoenixConfig, load_config
from self_heal.table_store import TableStore
from self_heal.logistics_store import LogisticsDatasetStore
from self_heal.telemetry import PhoenixTelemetry
from evals.analyst.generator import load_scenarios, prepare_case
from self_heal.storage import HistoryStore
from runner_transport import LocalProcessTransport
from evals.logistics.generator import public_incident_bundle


ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    def __init__(self):
        self.replies = iter([
            ModelReply(None, (ToolCall("one", "read_rows", json.dumps({
                "limit": 4, "filter_field": "warehouse", "filter_value": "East",
            })),), 20),
            ModelReply('{"value":18}', (), 20),
        ])

    def complete(self, messages, tools):
        return next(self.replies)


def test_daytona_protocol_bridge_counts_model_and_table_work_outside_candidate(monkeypatch):
    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase()
    store = TableStore(database, config)
    fixture = json.loads((ROOT / "evals" / "analyst" / "data" / "small_inventory.json").read_text())
    store.materialize("small", fixture["rows"])
    runner = CandidateRunner(
        store=store, config=config, history=None, transport_factory=LocalProcessTransport,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
    )
    runner.transport_factory = LocalProcessTransport
    table = store.open_session("small")
    task = {"metric": "available", "filter_field": "warehouse", "filter_value": "East"}
    result = runner._execute_sandbox(ROOT / "harness", task, "run-1", table,
                                       ScriptedModel(), AnalystTools(table, config))
    assert result.outcome == "answered"
    assert result.answer == {"value": 18}
    assert result.model_calls == 2
    assert result.tool_calls == 1
    assert result.table_pages == 1
    assert result.total_tokens == 40


def test_daytona_protocol_bridge_runs_pinned_logistics_agent_through_scoped_bundle(monkeypatch):
    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase()
    inventory = TableStore(database, config)
    logistics = LogisticsDatasetStore(database)
    logistics.ensure_indexes()
    dataset = logistics.materialize("logistics", **public_incident_bundle())
    runner = CandidateRunner(
        store=inventory, logistics=logistics, config=config, history=None,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
    )
    runner.transport_factory = LocalProcessTransport
    table = logistics.open_session(dataset.dataset_id)
    from test_logistics import logistics_model
    question = "Count senders who shipped over fifteen times yesterday from depot three"
    result = runner._execute_sandbox(
        ROOT / "harness", question, "logistics-run", table, logistics_model(), LogisticsTools(table, config),
        input_kind="logistics_bundle",
    )
    assert result.outcome == "answered", result.error
    assert result.answer == {"value": 2}
    assert result.model_calls == 2 and result.tool_calls == 1
    assert result.table_pages > 1
    assert table.completed_shipment_scan(3, "yesterday")


def test_container_receives_only_controlled_imports():
    support = ROOT / "runner_support"
    assert not (support / "evals").exists()
    assert not (support / "self_heal" / "storage.py").exists()
    assert not (support / "self_heal" / "evaluation.py").exists()


def test_full_protocol_bridge_enforces_the_assigned_table():
    config = load_config(ROOT / "config" / "analyst.yaml")
    store = TableStore(SQLiteDatabase(), config)
    fixture = json.loads((ROOT / "evals" / "analyst" / "data" / "small_inventory.json").read_text())
    store.materialize("small", fixture["rows"])
    runner = CandidateRunner(
        store=store, config=config, history=None, transport_factory=LocalProcessTransport,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
    )
    table = store.open_session("small")
    task = {"metric": "available", "filter_field": "warehouse", "filter_value": "East"}
    result = runner._execute_sandbox(ROOT / "harness", task, "sandbox-run", table,
                                       ScriptedModel(), AnalystTools(table, config))
    assert result.outcome == "answered", result.error
    assert result.answer == {"value": 18}
    assert result.table_pages == 1


def test_full_protocol_bridge_allows_candidate_owned_bulk_scan():
    config = load_config(ROOT / "config" / "analyst.yaml")
    store = TableStore(SQLiteDatabase(), config)
    scenario = load_scenarios(ROOT / "evals" / "analyst" / "scenarios.yaml", config)["bulk-warehouse-available"]
    case = prepare_case(scenario, config)
    store.materialize("bulk", list(case.rows))
    temp = tempfile.TemporaryDirectory()
    source = Path(temp.name) / "harness"
    Path(temp.name).chmod(0o755)
    shutil.copytree(ROOT / "harness", source)
    with (source / "tools.py").open("a") as file:
        file.write('''

_BaseTools = AnalystTools

class AnalystTools(_BaseTools):
    def definitions(self):
        return super().definitions() + [{
            "type": "function", "function": {"name": "aggregate",
            "description": "Aggregate a whole assigned table through bounded pages.",
            "parameters": {"type": "object", "properties": {
                "metric": {"type": "string"}, "group_by": {"type": "string"}},
            "required": ["metric", "group_by"]}}}
        ]

    def execute(self, name, arguments):
        if name != "aggregate":
            return super().execute(name, arguments)
        totals = {}
        cursor = None
        while True:
            page = self.table.read_rows(cursor=cursor, limit=4)
            for row in page["rows"]:
                value = row["on_hand"] - row["reserved"] if arguments["metric"] == "available" else row[arguments["metric"]]
                key = row[arguments["group_by"]]
                totals[key] = totals.get(key, 0) + value
            cursor = page["next_cursor"]
            if cursor is None:
                return {"groups": {key: totals[key] for key in sorted(totals)}}
''')

    class BulkModel:
        def __init__(self):
            self.replies = iter([
                ModelReply(None, (ToolCall("aggregate", "aggregate",
                                            '{"metric":"available","group_by":"warehouse"}'),), 20),
                ModelReply(json.dumps(case.expected_answer), (), 20),
            ])

        def complete(self, messages, tools):
            return next(self.replies)

    runner = CandidateRunner(
        store=store, config=config, history=None, transport_factory=LocalProcessTransport,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
    )
    table = store.open_session("bulk")
    result = runner._execute_sandbox(source, scenario.task, "bulk-run", table,
                                       BulkModel(), AnalystTools(table, config))
    assert result.outcome == "answered", result.error
    assert result.answer == case.expected_answer
    assert result.model_calls == 2 and result.tool_calls == 1
    assert result.table_pages == 128
    temp.cleanup()


def test_pinned_commit_run_records_supervisor_measured_evidence():
    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase()
    store = TableStore(database, config)
    history = HistoryStore(database)
    history.ensure_indexes()
    fixture = json.loads((ROOT / "evals" / "analyst" / "data" / "small_inventory.json").read_text())
    dataset = store.materialize("small", fixture["rows"])
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory)
        source.chmod(0o755)
        shutil.copytree(ROOT / "harness", source / "harness")
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["git", "add", "."], cwd=source, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@local.invalid",
                        "commit", "-qm", "source"], cwd=source, check=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        runner = CandidateRunner(
            store=store, config=config, history=history, transport_factory=LocalProcessTransport,
            telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
        )
        task = {"metric": "available", "filter_field": "warehouse", "filter_value": "East"}
        runner.image_identity = lambda: "daytona:test-snapshot:runtime-sha256"
        execution = runner.run(source=source, source_commit=commit, dataset=dataset,
                               invocation=task, model=ScriptedModel())
    assert execution.result.answer == {"value": 18}
    assert execution.history_status == "recorded"
    run = history.get_run(execution.result.run_id)
    assert run["execution"]["source"]["commit"] == commit
    assert run["execution"]["runtime"]["isolation"] == "daytona"
    assert run["execution"]["runtime"]["image_digest"].startswith("daytona:")
    assert run["resources"]["table_pages"] == 1


def test_sandbox_preserves_model_reported_capability_gap():
    from test_logistics import ScriptedModel as ReplyModel

    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase()
    logistics = LogisticsDatasetStore(database)
    dataset = logistics.materialize("logistics-gap", **public_incident_bundle())
    table = logistics.open_session(dataset.dataset_id)
    runner = CandidateRunner(
        store=TableStore(database, config), logistics=logistics, config=config, history=None,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
    )
    runner.transport_factory = LocalProcessTransport
    question = "Break down monthly shipping activity by segment."
    model = ReplyModel([ModelReply(json.dumps({"unsupported": {
        "reason": "Monthly aggregation is unavailable",
        "capability_request": {"operation": "monthly_shipments_by_segment"},
    }}), (), 10)])
    result = runner._execute_sandbox(ROOT / "harness", question, "gap-run", table, model,
                                     LogisticsTools(table, config), input_kind="logistics_bundle")
    assert result.outcome == "unsupported", result.error
    assert result.limitation_kind == "capability_gap"
    assert result.capability_request == {"operation": "monthly_shipments_by_segment", "question": question}
    assert result.model_calls == 1 and result.tool_calls == 0


def test_sandbox_can_use_a_new_tool_without_a_question_matcher(tmp_path):
    from test_logistics import ScriptedModel as ReplyModel

    source = tmp_path / "harness"
    shutil.copytree(ROOT / "harness", source)
    with (source / "logistics.py").open("a") as file:
        file.write('''

_BaseTools = LogisticsTools

class LogisticsTools(_BaseTools):
    def definitions(self):
        return super().definitions() + [{"type": "function", "function": {
            "name": "shipment_row_count", "description": "Count all shipment records in the bundle.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]

    def execute(self, name, arguments):
        if name == "shipment_row_count" and arguments == {}:
            return {"value": self.table.inspect_relation("shipments")["row_count"]}
        return super().execute(name, arguments)
''')
    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase()
    logistics = LogisticsDatasetStore(database)
    dataset = logistics.materialize("new-tool", **public_incident_bundle())
    table = logistics.open_session(dataset.dataset_id)
    runner = CandidateRunner(
        store=TableStore(database, config), logistics=logistics, config=config, history=None,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
    )
    runner.transport_factory = LocalProcessTransport
    model = ReplyModel([
        ModelReply(None, (ToolCall("count", "shipment_row_count", "{}"),), 10),
        ModelReply('{"task":{"operation":"shipment_row_count"},"answer":{"value":74}}', (), 10),
    ])
    result = runner._execute_sandbox(source, "Count all shipment records.", "new-tool-run", table, model,
                                     LogisticsTools(table, config), input_kind="logistics_bundle")
    assert result.outcome == "answered", result.error
    assert result.answer == {"value": 74}
    assert result.interpreted_task == {"operation": "shipment_row_count"}
    assert result.model_calls == 2 and result.tool_calls == 1


def test_relational_candidate_bridge_requires_a_complete_scan(tmp_path):
    from test_logistics import ScriptedModel as ReplyModel
    from evals.logistics.relational import reference_answer

    source = tmp_path / "harness"
    shutil.copytree(ROOT / "harness", source)
    with (source / "logistics.py").open("a") as file:
        file.write('''
_BaseTools = LogisticsTools
class LogisticsTools(_BaseTools):
    def definitions(self):
        return super().definitions() + [{"type": "function", "function": {
            "name": "count_relation", "description": "Count rows in a relation.",
            "parameters": {"type": "object", "properties": {
                "relation": {"type": "string"}, "partial": {"type": "boolean"}},
                "required": ["relation", "partial"], "additionalProperties": False}}}]
    def execute(self, name, arguments):
        if name != "count_relation":
            return super().execute(name, arguments)
        cursor, count = None, 0
        for _ in range(self.config.limits.max_pages):
            page = self.table.read_relation(relation=arguments["relation"], limit=3, cursor=cursor)
            count += len(page["rows"])
            cursor = page["next_cursor"]
            if cursor is None or arguments["partial"]:
                return {"value": count}
        raise ValueError("Page budget exceeded")
''')
    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase()
    logistics = LogisticsDatasetStore(database)
    bundle = public_incident_bundle()
    dataset = logistics.materialize("aggregate", **bundle)
    runner = CandidateRunner(store=TableStore(database, config), logistics=logistics, config=config, history=None,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")), transport_factory=LocalProcessTransport)
    for relation in ("customers", "warehouses", "shipments"):
        task = {"operation": "relational_aggregate", "relation": relation, "aggregate": "count",
                "field": None, "filters": [], "group_by": None}
        answer = reference_answer(bundle, task)
        for partial in (False, True):
            table = logistics.open_session(dataset.dataset_id)
            model = ReplyModel([
                ModelReply(None, (ToolCall("count", "count_relation", json.dumps({"relation": relation, "partial": partial})),), 10),
                ModelReply(json.dumps({"task": task, "answer": answer}), (), 10)])
            result = runner._execute_sandbox(source, "Count all " + relation, "aggregate-run", table, model,
                LogisticsTools(table, config), input_kind="logistics_bundle")
            if partial and relation != "warehouses":
                assert result.outcome == "error" and "not fully read" in result.error
            else:
                assert result.outcome == "answered", result.error
                assert result.answer == answer
