import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harness.tools import AnalystTools
from self_heal.database import SQLiteDatabase
from self_heal.evaluation import EvaluationRunner
from self_heal.execution import RunExecutor
from self_heal.model import ModelReply, ToolCall
from self_heal.settings import PhoenixConfig, load_config
from self_heal.storage import HistoryStore, HistoryError
from self_heal.table_store import TableStore
from self_heal.telemetry import PhoenixTelemetry, redact_trace_payload
from evals.analyst.generator import load_scenarios, prepare_case


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)

    def complete(self, messages, tools):
        return next(self.replies)


class FakePhoenix:
    def __init__(self, exporter, retrievable=True):
        self.exporter, self.retrievable, self.annotations = exporter, retrievable, []
        self.spans = self
        self.projects = SimpleNamespace(get=lambda **kw: {"id": "project-1"})

    def get_spans(self, *, trace_ids, **kwargs):
        if not self.retrievable:
            return []
        results = []
        for span in self.exporter.get_finished_spans():
            trace_id = format(span.context.trace_id, "032x")
            if trace_id not in trace_ids:
                continue
            results.append(
                {
                    "name": span.name,
                    "context": {
                        "trace_id": trace_id,
                        "span_id": format(span.context.span_id, "016x"),
                    },
                    "parent_id": format(span.parent.span_id, "016x")
                    if span.parent
                    else None,
                    "attributes": dict(span.attributes),
                    "span_kind": span.attributes["openinference.span.kind"],
                    "status_code": span.status.status_code.name,
                    "start_time": datetime.fromtimestamp(
                        span.start_time / 1e9, timezone.utc
                    ).isoformat(),
                    "end_time": datetime.fromtimestamp(
                        span.end_time / 1e9, timezone.utc
                    ).isoformat(),
                }
            )
        return results

    def add_span_annotation(self, **kwargs):
        self.annotations.append(kwargs)
        return {"id": "annotation"}


def make_executor(*, enabled=True, retrievable=True, history=None):
    config = load_config()
    database = SQLiteDatabase()
    tables = TableStore(database, config)
    tables.ensure_indexes()
    fixture = Path("evals/analyst/data/small_inventory.json")
    tables.materialize("small", json.loads(fixture.read_text())["rows"])
    history = history or HistoryStore(database)
    history.ensure_indexes()
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = FakePhoenix(exporter, retrievable)
    telemetry = PhoenixTelemetry(
        PhoenixConfig(enabled=enabled, project="tests"),
        provider=provider,
        client=client,
        verification_attempts=1,
        retry_delay_seconds=0,
    )
    return (
        RunExecutor(history=history, telemetry=telemetry, config=config),
        tables,
        config,
        history,
        exporter,
        client,
    )


def supported_model():
    return ScriptedModel(
        [
            ModelReply(
                '{"metric":"available","filter_field":"warehouse","filter_value":"East"}',
                (),
                10,
            ),
            ModelReply(
                None,
                (
                    ToolCall(
                        "read-east",
                        "read_rows",
                        '{"limit":4,"filter_field":"warehouse","filter_value":"East"}',
                    ),
                ),
                10,
            ),
            ModelReply('{"value":18}', (), 10),
        ]
    )


def run_question(executor, tables, config, model, question):
    return executor.run(
        model=model,
        tools=AnalystTools(tables.open_session("small"), config),
        dataset=tables.dataset_info("small"),
        invocation=question,
    )


def test_trace_links_run_to_phoenix_and_sqlite_with_model_and_tool_children():
    executor, tables, config, history, exporter, client = make_executor()
    execution = run_question(
        executor,
        tables,
        config,
        supported_model(),
        "How many available units are in East?",
    )
    assert execution.result.answer == {"value": 18}
    assert execution.trace.status == "available"
    assert len(execution.trace.trace_id) == 32
    assert execution.trace.url.endswith(execution.trace.trace_id)
    spans = exporter.get_finished_spans()
    assert len([s for s in spans if s.name == "analyst.model"]) == 3
    assert [s.name for s in spans if s.name.startswith("tool.")] == ["tool.read_rows"]
    root = next(s for s in spans if s.name == "analyst.run")
    assert root.attributes["seal.run_id"] == execution.result.run_id
    assert all(s.context.trace_id == root.context.trace_id for s in spans)
    record = history.get_run(execution.result.run_id)
    assert record["trace"]["root_id"] == execution.trace.trace_id
    assert record["resources"]["table_pages"] == 1
    assert executor.telemetry.trace_spans(execution.trace.trace_id)
    assert executor.telemetry.read_redacted_trace(execution.trace.trace_id)


def test_redaction_precedes_export_and_handles_nested_serialized_payloads():
    value = redact_trace_payload(
        {
            "api_key": "sk-live-secret-value",
            "connection_string": "https://user:password@host",
            "result": {"rows": [{"sku": "A-100", "on_hand": 12}]},
            "message": {"content": json.dumps({"rows": [{"sku": "A-100"}]})},
            "answer": {"value": 18},
        }
    )
    assert not any(
        secret in repr(value)
        for secret in ["sk-live-secret-value", "password", "A-100"]
    )
    executor, tables, config, _, exporter, _ = make_executor()
    run_question(
        executor,
        tables,
        config,
        supported_model(),
        "How many available units are in East?",
    )
    exported = repr([dict(s.attributes) for s in exporter.get_finished_spans()])
    assert "A-100" not in exported
    assert "redacted_row_count" in exported


def test_disabled_or_missing_remote_trace_preserves_answer_without_reexecution():
    for enabled, retrievable, status in [
        (False, True, "disabled"),
        (True, False, "incomplete"),
    ]:
        executor, tables, config, _, exporter, _ = make_executor(
            enabled=enabled, retrievable=retrievable
        )
        execution = run_question(
            executor,
            tables,
            config,
            supported_model(),
            "How many available units are in East?",
        )
        assert execution.result.answer == {"value": 18}
        assert execution.trace.status == status
        if not enabled:
            assert not exporter.get_finished_spans()


def test_unsupported_question_records_capability_gap_without_tool_execution():
    executor, tables, config, history, exporter, _ = make_executor()
    execution = run_question(
        executor,
        tables,
        config,
        ScriptedModel(
            [
                ModelReply(
                    '{"error":"Revenue is outside the inventory contract"}', (), 10
                )
            ]
        ),
        "What is total revenue?",
    )
    assert execution.result.outcome == "unsupported"
    assert execution.result.tool_calls == 0
    assert len(history.capability_gaps()) == 1
    assert not [s for s in exporter.get_finished_spans() if s.name.startswith("tool.")]


def test_history_failure_preserves_answer():
    class FailingHistory(HistoryStore):
        def start_run(self, record):
            raise HistoryError("offline")

    executor, tables, config, _, _, _ = make_executor(
        history=FailingHistory(SQLiteDatabase())
    )
    execution = run_question(
        executor,
        tables,
        config,
        supported_model(),
        "How many available units are in East?",
    )
    assert execution.result.answer == {"value": 18}
    assert execution.history_status == "incomplete"


def test_protected_evaluation_is_published_as_code_annotation():
    executor, tables, config, history, _, client = make_executor()
    case = prepare_case(
        load_scenarios(Path("evals/analyst/scenarios.yaml"), config)[
            "small-east-available"
        ],
        config,
    )
    history.evaluation_sink = executor.telemetry.publish_evaluation
    trial = EvaluationRunner(
        tables, config, executor=executor, history=history
    ).run_case(case, supported_model())
    assert trial.passed
    (annotation,) = client.annotations
    assert annotation["score"] == 1
    assert annotation["annotator_kind"] == "CODE"
    assert annotation["metadata"]["case_id"] == trial.case_id
    assert (
        history.evaluations.find_one({"trial_id": trial.trial_id})["phoenix"]["status"]
        == "available"
    )


def test_root_span_finalization_failure_never_replays_completed_model_calls(
    monkeypatch,
):
    from self_heal import telemetry as module

    executor, tables, config, _, _, _ = make_executor()
    original = module._PhoenixSpan.end

    def fail_root(self, *, outputs):
        if "outcome" in outputs:
            raise RuntimeError("span finalization failed")
        return original(self, outputs=outputs)

    monkeypatch.setattr(module._PhoenixSpan, "end", fail_root)
    execution = run_question(
        executor,
        tables,
        config,
        supported_model(),
        "How many available units are in East?",
    )
    assert execution.result.answer == {"value": 18}
    assert execution.trace.status == "incomplete"
    assert execution.trace.error_type == "RuntimeError"


def test_telemetry_initialization_failure_runs_agent_once(monkeypatch):
    executor, tables, config, _, _, _ = make_executor()

    def fail():
        raise ConnectionError("offline")

    monkeypatch.setattr(executor.telemetry, "_dependencies", fail)
    execution = run_question(
        executor,
        tables,
        config,
        supported_model(),
        "How many available units are in East?",
    )
    assert execution.result.answer == {"value": 18}
    assert execution.trace.status == "incomplete"
    assert execution.trace.error_type == "ConnectionError"


def test_supervisor_structured_calls_are_observed_as_llm_spans():
    executor, _, _, _, exporter, _ = make_executor()
    received = []
    model = SimpleNamespace(
        model="evolution-test",
        complete_json=lambda messages, schema: (
            received.append(schema) or ModelReply('{"diff":"patch"}', total_tokens=17)
        ),
    )
    observed = executor.telemetry.observe_model(model)
    assert (
        observed.complete_json(
            [{"role": "user", "content": "Propose a repair"}], {"type": "object"}
        ).content
        == '{"diff":"patch"}'
    )
    (span,) = exporter.get_finished_spans()
    assert span.name == "evolution.model"
    assert span.attributes["openinference.span.kind"] == "LLM"
    assert received == [{"type": "object"}]
