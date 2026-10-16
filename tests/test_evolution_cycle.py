import uuid
from dataclasses import replace
import json
import subprocess
from pathlib import Path

from self_heal.database import SQLiteDatabase

from harness.agent import RunResult
from self_heal.evaluation import SelectionEvaluator
from self_heal.execution import RunExecution
from self_heal.settings import load_config
from self_heal.storage import HistoryStore
from self_heal.storage import HistoryError
import pytest
from self_heal.table_store import TableStore
from self_heal.telemetry import TraceEvidence
from self_heal.controller import EvolutionController
from self_heal.repository import CandidateRepository
from self_heal.model import ModelReply
from self_heal.evolution import propose_scenario


class Model:
    model = "fixed-scripted-model"


class ScriptedCandidateRunner:
    def __init__(self, store, wrong_validation=False, wrong_regression=False):
        self.store = store
        self.wrong_validation = wrong_validation
        self.wrong_regression = wrong_regression

    def image_identity(self):
        return "sha256:" + "a" * 64

    def run(
        self,
        *,
        source,
        source_commit,
        dataset,
        invocation,
        model,
        case_id,
        case_exposure,
    ):
        is_candidate = source_commit == "candidate"
        if case_exposure == "negative_refusal":
            outcome, answer, interpreted, limitation = (
                "unsupported",
                None,
                None,
                "capability_gap",
            )
        elif not is_candidate and case_exposure == "original":
            outcome, answer, interpreted, limitation = (
                "unsupported",
                None,
                None,
                "capability_gap",
            )
        else:
            from evals.analyst.oracle import reference_answer

            task = self.tasks[case_id]
            answer = reference_answer(
                self.store.verified_rows(dataset.dataset_id), task, self.config
            )
            if (
                self.wrong_validation
                and is_candidate
                and case_exposure == "private_validation"
            ):
                answer = {"value": -1}
            if self.wrong_regression and is_candidate and case_exposure == "regression":
                answer = {"value": -1}
            outcome, interpreted, limitation = "answered", task, None
        run_id = str(uuid.uuid4())
        result = RunResult(
            run_id,
            answer,
            None,
            outcome,
            interpreted,
            2,
            1,
            40,
            0.1,
            1,
            100,
            limitation,
            "unsupported" if limitation else None,
        )
        trace = TraceEvidence(run_id, None, "available", "test", None, None)
        return RunExecution(result, trace, "recorded")


def setup_selection(wrong_validation=False, wrong_regression=False):
    config = load_config()
    db = SQLiteDatabase()
    store = TableStore(db, config)
    history = HistoryStore(db)
    history.ensure_indexes()
    rows = [
        {
            "sku": "A",
            "warehouse": "Cedar",
            "category": "Tools",
            "on_hand": 10,
            "reserved": 2,
        },
        {
            "sku": "B",
            "warehouse": "Harbor",
            "category": "Parts",
            "on_hand": 6,
            "reserved": 1,
        },
    ]
    original_data = store.materialize("incident", rows)
    regression_data = store.materialize("regression", list(reversed(rows)))
    runner = ScriptedCandidateRunner(store, wrong_validation, wrong_regression)
    runner.config = config
    runner.tasks = {}
    evaluator = SelectionEvaluator(runner, history, config)
    original_task = {"metric": "available", "group_by": "warehouse"}
    original = evaluator.freeze_case(
        scenario_id="incident",
        dataset=original_data,
        invocation="What are the available units in each warehouse?",
        task=original_task,
        role="original",
        origin="observed_incident",
    )
    regression = evaluator.freeze_case(
        scenario_id="regression",
        dataset=regression_data,
        invocation={"metric": "reserved"},
        task={"metric": "reserved"},
        role="regression",
        origin="declared_scenario",
    )
    validation = evaluator.private_validation_cases(original_task)
    negative = evaluator.negative_refusal(regression_data)
    runner.tasks = {
        case.case_id: case.task for case in [original, regression, *validation]
    }
    return evaluator, history, original, regression, validation, negative


def select(evaluator, original, regression, validation, negative):
    return evaluator.select(
        candidate_id="candidate-id",
        parent_commit="parent",
        candidate_commit="candidate",
        parent_source=Path("/parent"),
        candidate_source=Path("/candidate"),
        original=original,
        regressions=[regression],
        validation=validation,
        negative=negative,
        model_factory=Model,
        observed_violation="capability_refusal_for_answerable_case",
    )


def test_frozen_plan_and_repeated_trials_accept_only_correct_transfer():
    evaluator, history, original, regression, validation, negative = setup_selection()
    decision = select(evaluator, original, regression, validation, negative)
    assert decision.accepted
    assert len(decision.trials) == 2 * 2 + 2 + 4 * 2 * 2 + 2
    plan = history.selection_plans.find_one({"_id": decision.plan_id})
    assert plan["candidate_commit"] == "candidate"
    assert all(case["dataset_hash"] for case in plan["cases"])
    assert history.evaluations.count_documents({"plan_id": decision.plan_id}) == len(
        decision.trials
    )


def test_hardcoded_original_answer_fails_private_variations():
    evaluator, _, original, regression, validation, negative = setup_selection(
        wrong_validation=True
    )
    decision = select(evaluator, original, regression, validation, negative)
    assert not decision.accepted
    assert "validation_failed_or_flaky" in decision.reasons


def test_regression_and_changed_dataset_identity_are_rejected():
    evaluator, _, original, regression, validation, negative = setup_selection(
        wrong_regression=True
    )
    decision = select(evaluator, original, regression, validation, negative)
    assert "regression_failed" in decision.reasons
    with pytest.raises(HistoryError, match="Dataset changed"):
        evaluator.freeze_case(
            scenario_id="tampered",
            dataset=replace(original.dataset, content_hash="wrong"),
            invocation=original.invocation,
            task=original.task,
            role="original",
            origin="observed_incident",
        )


class ProposalModel:
    model = "proposal-script"

    def __init__(self, replies):
        self.replies = iter(replies)

    def complete(self, messages, tools):
        return ModelReply(json.dumps(next(self.replies)))


class TraceReader:
    def read_redacted_trace(self, trace_id):
        return [{"name": "analyst.run", "outputs": {"outcome": "unsupported"}}]


def test_retry_feedback_explains_failed_tools_without_exposing_holdouts_or_values():
    history = HistoryStore(SQLiteDatabase())
    controller = object.__new__(EvolutionController)
    controller.history = history
    for role, version, passed in [
        ("original", "candidate", False),
        ("generated_reproduction", "candidate", False),
        ("private_validation", "candidate", False),
        ("original", "baseline", False),
        ("original", "candidate", True),
    ]:
        run_id = str(uuid.uuid4())
        history.evaluations.insert_one(
            {
                "candidate": {"candidate_id": "repair", "version": version},
                "run_id": run_id,
                "passed": passed,
                "case_role": role,
                "violation": "model_call_budget_exhausted",
                "actual": {
                    "answer": {"value": 987654},
                    "error": "Model-call budget exceeded",
                },
            }
        )
        history.runs.insert_one(
            {
                "run_id": run_id,
                "evidence": {
                    "tool_calls": [
                        {
                            "name": "aggregate_metric",
                            "arguments": {"filter_value": "", "api_key": "hidden-key"},
                            "result": {"groups": {"East": 987654}},
                        },
                        {
                            "name": "read_rows",
                            "result": {"rows": [{"sku": "hidden-row"}]},
                        },
                    ]
                },
            }
        )
    feedback = controller._failed_development_trials("repair")
    assert [item["role"] for item in feedback] == ["original", "generated_reproduction"]
    assert feedback[0]["tool_calls"][0]["arguments"]["filter_value"] == ""
    serialized = json.dumps(feedback)
    assert all(
        secret not in serialized
        for secret in ["987654", "hidden-key", "hidden-row", "private_validation"]
    )
    assert "redacted_row_count" in serialized


class CycleRunner(ScriptedCandidateRunner):
    def __init__(self, store, history, config, parent):
        super().__init__(store)
        self.history, self.config, self.parent = history, config, parent

    def run(
        self,
        *,
        source,
        source_commit,
        dataset,
        invocation,
        model,
        case_id,
        case_exposure,
    ):
        case = self.history.get_eval_case(case_id)
        task = case["task"]
        if case_exposure == "negative_refusal" or (
            source_commit == self.parent
            and case_exposure in {"reproduction", "original", "generated_reproduction"}
        ):
            outcome, answer, interpreted, limitation = (
                "unsupported",
                None,
                None,
                "capability_gap",
            )
        else:
            from evals.analyst.oracle import reference_answer

            answer = reference_answer(
                self.store.verified_rows(dataset.dataset_id), task, self.config
            )
            outcome, interpreted, limitation = "answered", task, None
        run_id = str(uuid.uuid4())
        result = RunResult(
            run_id,
            answer,
            None,
            outcome,
            interpreted,
            2,
            1,
            40,
            0.1,
            1,
            100,
            limitation,
            "unsupported" if limitation else None,
        )
        return RunExecution(
            result,
            TraceEvidence(run_id, None, "available", "test", None, None),
            "recorded",
        )


def cycle_setup(tmp_path, question):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "tools.py").write_text("VALUE = 1\n")
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
    config = load_config()
    db = SQLiteDatabase()
    store = TableStore(db, config)
    history = HistoryStore(db)
    history.ensure_indexes()
    dataset = store.materialize(
        "observed",
        [
            {
                "sku": "A",
                "warehouse": "Cedar",
                "category": "Tools",
                "on_hand": 10,
                "reserved": 2,
            },
            {
                "sku": "B",
                "warehouse": "Harbor",
                "category": "Parts",
                "on_hand": 6,
                "reserved": 1,
            },
        ],
    )
    run_id = str(uuid.uuid4())
    history.runs.insert_one(
        {
            "_id": run_id,
            "run_id": run_id,
            "status": "completed",
            "outcome": "unsupported",
            "limitation_kind": "capability_gap",
            "limitation_reason": "Question is ambiguous or unsupported",
            "invocation": {"question": question},
            "dataset": {
                "id": dataset.dataset_id,
                "content_hash": dataset.content_hash,
                "row_count": dataset.row_count,
            },
            "trace": {"status": "available", "root_id": run_id},
            "execution": {"source": {"commit": parent, "dirty": False}},
            "resources": {"model_calls": 1},
        }
    )
    return store, history, config, parent, run_id


def test_controller_leaves_ungradable_external_capability_open(tmp_path):
    store, history, config, parent, run_id = cycle_setup(
        tmp_path, "What is total revenue?"
    )
    repo = CandidateRepository(tmp_path)
    controller = EvolutionController(
        store=store,
        history=history,
        config=config,
        telemetry=TraceReader(),
        repository=repo,
        runner=CycleRunner(store, history, config, parent),
        model_factory=Model,
        evolution_model=ProposalModel(
            [
                {
                    "contract_extension": {
                        "behavior": "sum revenue",
                        "data_access": "unit prices",
                        "oracle": "trusted price total",
                    }
                }
            ]
        ),
    )
    result = controller.evolve(run_id)
    assert result["status"] == "needs_contract"
    assert history.get_gap(run_id)["status"] == "needs_contract"
    assert history.candidates.count_documents({}) == 0


def test_scenario_proposer_corrects_a_fenced_but_invalid_task():
    config = load_config()
    invalid = {
        "description": "bulk",
        "task": {"metric": "available", "group_by": ["warehouse"]},
        "question": "What are available units in each warehouse?",
        "generation": {
            "seed": 1,
            "row_count": 128,
            "warehouses": ["Cedar"],
            "categories": ["Tools"],
            "on_hand": [1, 10],
        },
        "requested_capability": "bulk aggregation",
    }
    valid = {**invalid, "task": {"metric": "available", "group_by": "warehouse"}}

    class FencedModel(ProposalModel):
        def complete(self, messages, tools):
            return ModelReply("```json\n" + json.dumps(next(self.replies)) + "\n```")

    scenario, _ = propose_scenario(
        FencedModel([invalid, valid]),
        incident={"question": invalid["question"]},
        contract={"version": config.task_contract_version},
        config=config,
        scenario_id="retry",
    )
    assert scenario.task == valid["task"]


def test_controller_freezes_reproductions_and_activates_only_selected_commit(tmp_path):
    question = "What are the available units in each warehouse?"
    store, history, config, parent, run_id = cycle_setup(tmp_path, question)
    diff = """diff --git a/harness/tools.py b/harness/tools.py
--- a/harness/tools.py
+++ b/harness/tools.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    proposals = ProposalModel(
        [
            {
                "description": "Grouped bulk transfer",
                "task": {"metric": "available", "group_by": "warehouse"},
                "question": question,
                "generation": {
                    "seed": 9,
                    "row_count": 128,
                    "warehouses": ["Cedar", "Harbor"],
                    "categories": ["Tools", "Parts"],
                    "on_hand": [1, 20],
                },
                "requested_capability": "scan a larger table",
            },
            {
                "hypothesis": "A reusable scan can aggregate all rows",
                "changed_mechanism": "aggregate_scan",
                "diff": diff,
            },
        ]
    )
    controller = EvolutionController(
        store=store,
        history=history,
        config=config,
        telemetry=TraceReader(),
        repository=CandidateRepository(tmp_path),
        runner=CycleRunner(store, history, config, parent),
        model_factory=Model,
        evolution_model=proposals,
    )
    result = controller.evolve(run_id)
    assert result["status"] == "activated", result
    assert (
        history.active_version(config.task_family)["commit"]
        == result["candidate_commit"]
    )
    assert history.evaluations.count_documents({}) > 10
    assert history.get_gap(run_id)["status"] == "resolved"


def test_rescreen_reuses_stored_patch_and_frozen_failure(tmp_path):
    question = "What are the available units in each warehouse?"
    store, history, config, parent, run_id = cycle_setup(tmp_path, question)
    runner = CycleRunner(store, history, config, parent)
    controller = EvolutionController(
        store=store,
        history=history,
        config=config,
        telemetry=TraceReader(),
        repository=CandidateRepository(tmp_path),
        runner=runner,
        model_factory=Model,
        evolution_model=ProposalModel([]),
    )
    scenario_id = "observed-" + run_id.replace("-", "")[:24]
    dataset = store.dataset_info("observed")
    task = {"metric": "available", "group_by": "warehouse"}
    original = controller.evaluator.freeze_case(
        scenario_id=scenario_id + "-original",
        dataset=dataset,
        invocation=question,
        task=task,
        role="original",
        origin="observed_incident",
    )
    generated = controller.evaluator.freeze_case(
        scenario_id=scenario_id,
        dataset=dataset,
        invocation=question,
        task=task,
        role="generated_reproduction",
        origin="model_proposed_protected_oracle",
    )
    for case in (original, generated):
        execution = runner.run(
            source=tmp_path,
            source_commit=parent,
            dataset=dataset,
            invocation=question,
            model=Model(),
            case_id=case.case_id,
            case_exposure="reproduction",
        )
        controller._record_probe(
            case, execution, "capability_refusal_for_answerable_case", parent
        )
    history.record_gap(
        {"_id": run_id, "run_id": run_id, "status": "open", "created_at": "now"}
    )
    diff = """--- a/harness/tools.py
+++ b/harness/tools.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    history.record_candidate(
        {
            "_id": "old",
            "candidate_id": "old",
            "task_family": config.task_family,
            "changed_mechanism": "bounded_scan",
            "hypothesis": "Reusable scan",
            "diff": diff,
            "attempt": 2,
            "parent_commit": parent,
            "status": "rejected",
            "rejection_reason": "Git operation failed: corrupt patch",
            "created_at": "now",
        }
    )
    result = controller.rescreen(run_id, "old")
    assert result["status"] == "activated", result
    recovered = history.candidates.find_one({"rescreen_of": "old"})
    assert recovered["diff"] == diff and recovered["attempt"] == 2
    assert history.candidates.count_documents({"rescreen_of": "old"}) == 1
    with pytest.raises(Exception, match="not open"):
        controller.rescreen(run_id, "old")


def test_resume_reuses_frozen_generated_case_without_reproposing_dataset(tmp_path):
    question = "What are the available units in each warehouse?"
    store, history, config, parent, run_id = cycle_setup(tmp_path, question)
    controller = EvolutionController(
        store=store,
        history=history,
        config=config,
        telemetry=TraceReader(),
        repository=CandidateRepository(tmp_path),
        runner=CycleRunner(store, history, config, parent),
        model_factory=Model,
        evolution_model=ProposalModel(
            [
                {
                    "hypothesis": "Reusable scan",
                    "changed_mechanism": "bounded_scan",
                    "diff": "--- a/harness/tools.py\n+++ b/harness/tools.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
                }
            ]
        ),
    )
    scenario_id = "observed-" + run_id.replace("-", "")[:24]
    frozen = controller.evaluator.freeze_case(
        scenario_id=scenario_id,
        dataset=store.dataset_info("observed"),
        invocation=question,
        task={"metric": "available", "group_by": "warehouse"},
        role="generated_reproduction",
        origin="model_proposed_protected_oracle",
    )
    before = history.get_eval_case(frozen.case_id)["dataset"]
    result = controller.evolve(run_id)
    assert result["status"] == "activated", result
    assert history.get_eval_case(frozen.case_id)["dataset"] == before


@pytest.mark.parametrize(
    "baseline_error,expected_status",
    [
        ("Model final answer is not JSON", "activated"),
        ("Model or runtime failure: ConnectionError", "open"),
        ("Isolated candidate failure: ConnectionError", "open"),
    ],
)
def test_reproduction_grades_task_failure_and_excludes_infrastructure(
    tmp_path, baseline_error, expected_status
):
    from dataclasses import replace

    question = "What are the available units in each warehouse?"
    store, history, config, parent, run_id = cycle_setup(tmp_path, question)
    task = {"metric": "available", "group_by": "warehouse"}
    history.runs.update_one(
        {"_id": run_id},
        {
            "$set": {
                "outcome": "error",
                "error": "Model-call budget exceeded before a final answer",
                "interpreted_task": task,
            }
        },
    )

    class VaryingRunner(CycleRunner):
        def run(self, **kwargs):
            execution = super().run(**kwargs)
            if kwargs["source_commit"] == parent and kwargs["case_exposure"] in {
                "reproduction",
                "original",
                "generated_reproduction",
            }:
                execution = replace(
                    execution,
                    result=replace(
                        execution.result,
                        outcome="error",
                        error=baseline_error,
                        limitation_kind=None,
                    ),
                )
            return execution

    controller = EvolutionController(
        store=store,
        history=history,
        config=config,
        telemetry=TraceReader(),
        repository=CandidateRepository(tmp_path),
        runner=VaryingRunner(store, history, config, parent),
        model_factory=Model,
        evolution_model=ProposalModel(
            [
                {
                    "hypothesis": "Reusable scan",
                    "changed_mechanism": "bounded_scan",
                    "diff": "--- a/harness/tools.py\n+++ b/harness/tools.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
                }
            ]
        ),
    )
    controller.evaluator.freeze_case(
        scenario_id="observed-" + run_id.replace("-", "")[:24],
        dataset=store.dataset_info("observed"),
        invocation=question,
        task=task,
        role="generated_reproduction",
        origin="model_proposed_protected_oracle",
    )
    result = controller.evolve(run_id)
    assert result["status"] == expected_status, result
    if expected_status == "open":
        assert history.active_version(config.task_family) is None
