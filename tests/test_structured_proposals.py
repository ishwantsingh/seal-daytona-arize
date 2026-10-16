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


def test_full_file_proposal_computes_patch_against_exact_pinned_source(tmp_path):
    import subprocess
    from self_heal.evolution import _proposal_from_json
    from self_heal.repository import CandidateRepository

    (tmp_path / "harness").mkdir()
    (tmp_path / "harness/tools.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@local.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    parent = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    proposal = _proposal_from_json(
        {
            "hypothesis": "Reusable change",
            "changed_mechanism": "scan",
            "files": [
                {"path": "harness/tools.py", "content": "VALUE = 2\n"},
                {
                    "path": "harness/aggregate.py",
                    "content": "def total(values):\n    return sum(values)\n",
                },
            ],
        },
        {"tools.py": "VALUE = 1\n"},
    )
    candidate = CandidateRepository(tmp_path).apply_proposal(parent, proposal.diff)
    assert (candidate.worktree / "harness/tools.py").read_text() == "VALUE = 2\n"
    assert (candidate.worktree / "harness/aggregate.py").is_file()


@pytest.mark.parametrize(
    "files",
    [
        [{"path": "../tools.py", "content": "VALUE = 2"}],
        [{"path": "src/self_heal/evaluation.py", "content": "VALUE = 2"}],
        [{"path": "harness/tools.py", "content": "def broken("}],
        [
            {"path": "harness/tools.py", "content": "VALUE = 2"},
            {"path": "harness/tools.py", "content": "VALUE = 3"},
        ],
        [{"path": "harness/tools.py", "content": "VALUE = 1\n"}],
    ],
)
def test_invalid_full_file_proposals_fail_before_git_execution(files):
    from self_heal.evolution import _proposal_from_json

    with pytest.raises(ProposalError):
        _proposal_from_json(
            {"hypothesis": "h", "changed_mechanism": "m", "files": files},
            {"tools.py": "VALUE = 1\n"},
        )
