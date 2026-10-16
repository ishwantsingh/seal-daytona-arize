import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from self_heal.database import SQLiteDatabase
import pytest

from harness.agent import AnalystAgent
from harness.tools import AnalystTools, ToolError
from self_heal.cli import _answer_message, _parser, _present_run
from self_heal.model import ModelReply, OpenRouterModel, ToolCall
from self_heal.settings import load_config
from self_heal.table_store import TableStore


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "data" / "small_inventory.json"


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append((messages, tools))
        return next(self.replies)


def call(name, arguments, number):
    return ModelReply(None, (ToolCall(f"call-{number}", name, json.dumps(arguments)),), 100)


def make_agent(model, config=None):
    config = config or load_config()
    store = TableStore(SQLiteDatabase(), config)
    store.ensure_indexes()
    store.materialize("small", json.loads(FIXTURE.read_text())["rows"])
    table = store.open_session("small")
    tools = AnalystTools(table, config)
    return AnalystAgent(model, tools, config)


def test_scripted_agent_answers_with_only_three_registered_tools():
    model = ScriptedModel(
        [
            call("inspect_table", {}, 1),
            call("read_rows", {"limit": 4, "filter_field": "warehouse", "filter_value": "East"}, 2),
            call("calculate", {"operation": "sum", "values": [8, 5, 5]}, 3),
            ModelReply('{"value":18}', (), 100),
        ]
    )
    agent = make_agent(model)
    result = agent.run({"metric": "available", "filter_field": "warehouse", "filter_value": "East"})
    assert result.error is None
    assert result.answer == {"value": 18}
    assert result.model_calls == 4
    assert result.tool_calls == 3
    assert result.table_pages == 1
    assert {tool["function"]["name"] for tool in model.seen[0][1]} == {"inspect_table", "read_rows", "calculate"}
    assert model.seen[-1][0][-1]["role"] == "tool"


def test_natural_question_is_interpreted_then_answered_with_shared_budget():
    model = ScriptedModel(
        [
            ModelReply('{"metric":"available","filter_field":"warehouse","filter_value":"East"}', (), 50),
            call("inspect_table", {}, 1),
            call("read_rows", {"limit": 4, "filter_field": "warehouse", "filter_value": "East"}, 2),
            call("calculate", {"operation": "sum", "values": [8, 5, 5]}, 3),
            ModelReply('{"value":18}', (), 100),
        ]
    )
    result = make_agent(model).run("How many available units are in the East warehouse?")
    assert result.error is None
    assert result.answer == {"value": 18}
    assert result.interpreted_task == {"metric": "available", "filter_field": "warehouse", "filter_value": "East"}
    assert result.model_calls == 5
    assert result.total_tokens == 450
    assert model.seen[0][1] == []
    assert "Original question:" in model.seen[1][0][1]["content"]


def test_natural_question_rejects_ambiguous_or_invalid_interpretation():
    ambiguous = make_agent(ScriptedModel([ModelReply('{"error":"unclear metric"}', (), 40)])).run(
        "How much inventory do we have?"
    )
    assert ambiguous.outcome == "unsupported"
    assert ambiguous.error is None
    assert ambiguous.answer is None
    assert ambiguous.limitation_kind == "capability_gap"
    assert ambiguous.limitation_reason == "Question is ambiguous or unsupported"
    assert ambiguous.model_calls == 1
    assert ambiguous.tool_calls == 0
    unsupported = make_agent(ScriptedModel([ModelReply('{"metric":"revenue"}', (), 40)])).run(
        "What is the revenue?"
    )
    assert unsupported.outcome == "unsupported"
    assert unsupported.error is None
    assert unsupported.tool_calls == 0
    malformed = make_agent(ScriptedModel([ModelReply("[]", (), 40)])).run("What is the revenue?")
    assert malformed.outcome == "error"
    assert malformed.error == "Question interpretation has an invalid format"
    assert make_agent(ScriptedModel([])).run(" ").error == "Question must be 1 to 2000 characters"


def test_supervisor_can_supply_the_run_id_used_for_linked_evidence():
    result = make_agent(ScriptedModel([ModelReply('{"error":"unsupported"}', (), 1)])).run(
        "What is revenue?", run_id="linked-run-id"
    )
    assert result.run_id == "linked-run-id"


def test_question_interpretation_uses_the_same_model_call_limit():
    config = load_config()
    limited = replace(config, limits=replace(config.limits, max_model_calls=1))
    result = make_agent(ScriptedModel([ModelReply('{"metric":"on_hand"}')]), limited).run(
        "How many units are on hand?"
    )
    assert result.error == "Model-call budget exceeded before a final answer"
    assert result.model_calls == 1


def test_question_interpretation_uses_the_same_token_limit():
    config = load_config()
    limited = replace(config, limits=replace(config.limits, max_total_tokens=30))
    result = make_agent(ScriptedModel([ModelReply('{"metric":"on_hand"}', (), 40)]), limited).run(
        "How many units are on hand?"
    )
    assert result.error == "Token budget exceeded"
    assert result.model_calls == 1
    assert result.tool_calls == 0


def test_cli_accepts_exactly_one_question_or_structured_task():
    parser = _parser()
    assert parser.parse_args(["run", "--dataset", "small", "--question", "How many units?"]).question
    assert parser.parse_args(["run", "--dataset", "small", "--question", "How many units?", "--json"]).json
    assert parser.parse_args(["run", "--dataset", "small", "--task", '{"metric":"on_hand"}']).task
    assert parser.parse_args(["history", "capability-gaps", "--task-family", "inventory-totals"]).history_command == "capability-gaps"
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--dataset", "small"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--dataset", "small", "--question", "x", "--task", "{}"])


def test_question_cli_presents_answers_and_unsupported_questions_as_conversation(capsys):
    model = ScriptedModel(
        [
            ModelReply('{"metric":"available","filter_field":"warehouse","filter_value":"East"}'),
            call("read_rows", {"limit": 4, "filter_field": "warehouse", "filter_value": "East"}, 1),
            ModelReply('{"value":18}'),
        ]
    )
    answered = make_agent(model).run("How many available units are in East?")
    assert _present_run(answered, conversational=True, json_output=False) == 0
    assert capsys.readouterr().out == "There are 18 available units in the East warehouse.\n"

    unsupported = make_agent(ScriptedModel([ModelReply('{"error":"unsupported"}')])).run("What is the revenue?")
    assert _present_run(unsupported, conversational=True, json_output=False) == 0
    assert capsys.readouterr().out == "I can't answer that with my current capabilities.\n"

    assert _present_run(unsupported, conversational=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "unsupported"
    assert payload["answer"] is None
    assert payload["error"] is None
    assert payload["message"] == "I can't answer that with my current capabilities."

    failed = make_agent(ScriptedModel([ModelReply("not json")])).run({"metric": "on_hand"})
    assert _present_run(failed, conversational=True, json_output=False) == 1
    assert "couldn't complete" in capsys.readouterr().out


def test_conversational_answer_covers_grouped_and_zero_results():
    grouped = _answer_message(
        {"metric": "reserved", "group_by": "warehouse", "filter_field": "category", "filter_value": "Tools"},
        {"groups": {"East": 2, "West": 3}},
    )
    assert grouped == "Reserved units by warehouse in the Tools category: East: 2; West: 3."
    assert _answer_message(
        {"metric": "on_hand", "filter_field": "sku", "filter_value": "missing"},
        {"value": 0},
    ) == "There are 0 on-hand units for SKU missing."


def test_question_interpretation_request_omits_tool_options():
    model = OpenRouterModel.__new__(OpenRouterModel)
    completion = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"metric":"on_hand"}', tool_calls=None))],
        usage=SimpleNamespace(total_tokens=12),
    ))
    model.model = "test-model"
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    reply = model.complete([{"role": "user", "content": "How many units?"}], [])
    assert reply.content == '{"metric":"on_hand"}'
    assert reply.total_tokens == 12
    assert "tools" not in completion.call_args.kwargs
    assert "tool_choice" not in completion.call_args.kwargs


def test_model_call_limit_and_invalid_final_answer_fail_clearly():
    config = load_config()
    limited = replace(config, limits=replace(config.limits, max_model_calls=2))
    model = ScriptedModel([call("inspect_table", {}, 1), call("inspect_table", {}, 2)])
    result = make_agent(model, limited).run({"metric": "on_hand"})
    assert result.error == "Model-call budget exceeded before a final answer"
    assert result.answer is None
    invalid = make_agent(ScriptedModel([ModelReply("not json")])).run({"metric": "on_hand"})
    assert invalid.error == "Model final answer is not JSON"
    guessed = make_agent(ScriptedModel([ModelReply('{"value":18}')])).run({"metric": "available"})
    assert guessed.error == "Required table rows were not fully read"
    assert guessed.answer is None


def test_unregistered_tool_and_invalid_calculation_are_rejected():
    agent = make_agent(ScriptedModel([]))
    assert {tool["function"]["name"] for tool in agent.tools.definitions()} == {
        "inspect_table", "read_rows", "calculate"
    }
    try:
        agent.tools.execute("aggregate_rows", {})
    except ToolError as exc:
        assert "not registered" in str(exc)
    else:
        raise AssertionError("Unregistered tool was accepted")
    try:
        agent.tools.execute("read_rows", {"limit": 1, "dataset_id": "other"})
    except ToolError as exc:
        assert "Unsupported" in str(exc)
    else:
        raise AssertionError("Dataset switching was accepted")
    try:
        agent.tools.execute("calculate", {"operation": "difference", "values": [1, 2, 3]})
    except ToolError:
        pass
    else:
        raise AssertionError("Invalid calculation was accepted")
