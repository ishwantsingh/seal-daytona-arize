import json
from pathlib import Path

from self_heal.database import SQLiteDatabase

from evals.analyst.generator import load_scenarios, materialize_case, prepare_case
from harness.agent import RunResult
from self_heal.evaluation import EvaluationRunner, assess_run, baseline_expectation_matches
from self_heal.model import ModelReply, ToolCall
from self_heal.settings import load_config
from self_heal.table_store import TableStore


SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "scenarios.yaml"


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)

    def complete(self, messages, tools):
        return next(self.replies)


def tool_call(name, arguments, number):
    return ModelReply(None, (ToolCall(f"call-{number}", name, json.dumps(arguments)),), 10)


def make_runner():
    config = load_config()
    store = TableStore(SQLiteDatabase(), config)
    store.ensure_indexes()
    return EvaluationRunner(store, config), config


def case_for(config, scenario_id):
    return prepare_case(load_scenarios(SCENARIOS, config)[scenario_id], config)


def test_evaluator_passes_small_case_and_records_the_individual_trial():
    runner, config = make_runner()
    model = ScriptedModel(
        [
            ModelReply('{"metric":"available","filter_field":"warehouse","filter_value":"East"}', (), 10),
            tool_call("read_rows", {"limit": 4, "filter_field": "warehouse", "filter_value": "East"}, 1),
            ModelReply('{"value":18}', (), 10),
        ]
    )
    trial = runner.run_case(case_for(config, "small-east-available"), model)
    assert trial.passed is True
    assert trial.expected_answer == {"value": 18}
    assert trial.answer == {"value": 18}
    assert runner.trials == [trial]
    assert baseline_expectation_matches(trial, "pass") is True


def test_evaluator_marks_explicit_refusal_as_a_failed_answerable_case():
    runner, config = make_runner()
    trial = runner.run_case(
        case_for(config, "small-east-available"),
        ScriptedModel([ModelReply('{"error":"outside my capabilities"}', (), 10)]),
    )
    assert trial.outcome == "unsupported"
    assert trial.passed is False
    assert trial.violation == "capability_refusal_for_answerable_case"
    assert trial.table_pages == 0


def test_evaluator_rejects_wrong_and_missing_answers():
    runner, config = make_runner()
    wrong = runner.run_case(
        case_for(config, "small-east-available"),
        ScriptedModel(
            [
                ModelReply('{"metric":"available","filter_field":"warehouse","filter_value":"East"}'),
                tool_call("read_rows", {"limit": 4, "filter_field": "warehouse", "filter_value": "East"}, 1),
                ModelReply('{"value":17}'),
            ]
        ),
    )
    assert wrong.violation == "wrong_answer"
    missing = runner.run_case(
        case_for(config, "edge-empty-sku"),
        ScriptedModel(
            [
                ModelReply('{"metric":"reserved","filter_field":"sku","filter_value":"SKU-99999"}'),
                ModelReply(None),
            ]
        ),
    )
    assert missing.passed is False
    assert missing.violation == "agent_error"


def test_bulk_case_reliably_reaches_the_existing_model_call_budget():
    runner, config = make_runner()
    replies = [ModelReply('{"metric":"available","group_by":"warehouse"}', (), 10)]
    replies.extend(tool_call("read_rows", {"limit": 4}, number) for number in range(1, config.limits.max_model_calls))
    trial = runner.run_case(case_for(config, "bulk-warehouse-available"), ScriptedModel(replies))
    assert trial.passed is False
    assert trial.violation == "model_call_budget_exhausted"
    assert trial.model_calls == config.limits.max_model_calls
    assert baseline_expectation_matches(trial, "fails_model_call_budget") is True


def test_evaluator_rejects_a_result_that_exceeds_fixed_resource_limits():
    runner, config = make_runner()
    case = case_for(config, "small-east-available")
    materialized = materialize_case(runner.store, case)
    over_limit = RunResult(
        run_id="run",
        answer=case.expected_answer,
        error=None,
        outcome="answered",
        interpreted_task=case.scenario.task,
        model_calls=config.limits.max_model_calls + 1,
        tool_calls=0,
        total_tokens=0,
        elapsed_seconds=0,
        table_pages=0,
        table_bytes=0,
    )
    trial = assess_run(materialized, over_limit, config)
    assert trial.passed is False
    assert trial.violation == "model_call_limit_exceeded"
