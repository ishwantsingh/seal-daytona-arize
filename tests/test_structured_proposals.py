import json
from types import SimpleNamespace

import pytest

from self_heal.evolution import PATCH_SCHEMA, ProposalError, _request_json
from self_heal.model import ModelReply, OpenRouterModel


def test_supervisor_requests_strict_schema_without_altering_agent_settings():
    requests = []
    reply = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content='{"hypothesis":"h","changed_mechanism":"m","diff":"patch"}'
                ),
            )
        ],
        usage=SimpleNamespace(total_tokens=12),
    )
    model = OpenRouterModel.__new__(OpenRouterModel)
    model.model = "test"
    model.timeout_seconds = 120
    model.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: requests.append(kwargs) or reply
            )
        )
    )
    result = _request_json(model, "Return JSON", {}, schema=PATCH_SCHEMA)
    assert result["diff"] == "patch"
    (request,) = requests
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"] == PATCH_SCHEMA
    assert request["extra_body"]["provider"]["require_parameters"] is True
    assert "temperature" not in request
    assert model.tracing_settings["temperature"] == 0


def test_truncated_structured_reply_is_rejected_before_patch_screening():
    reply = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length", message=SimpleNamespace(content='{"diff":')
            )
        ],
        usage=None,
    )
    model = OpenRouterModel.__new__(OpenRouterModel)
    model.model = "test"
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: reply))
    )
    with pytest.raises(ProposalError, match="truncated"):
        _request_json(model, "Return JSON", {}, schema=PATCH_SCHEMA)


def test_scripted_models_still_use_the_same_validated_json_boundary():
    model = SimpleNamespace(
        complete=lambda messages, tools: ModelReply(
            json.dumps({"hypothesis": "h", "changed_mechanism": "m", "diff": "patch"})
        )
    )
    assert (
        _request_json(model, "Return JSON", {}, schema=PATCH_SCHEMA)["hypothesis"]
        == "h"
    )
