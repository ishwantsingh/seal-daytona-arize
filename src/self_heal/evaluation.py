"""Protected grading, frozen cases, and candidate selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import secrets
from pathlib import Path
from typing import Any, Callable, Iterable

from evals.analyst.generator import (
    MaterializedCase,
    PreparedCase,
    Scenario,
    materialize_case,
    prepare_case,
    load_scenarios,
)
from evals.analyst.oracle import reference_answer
from harness.agent import AnalystAgent, RunResult
from harness.tools import AnalystTools
from self_heal.contracts import (
    build_eval_case_record,
    build_evaluation_record,
    new_trial_id,
    stable_case_id,
    utc_now,
    canonical_hash,
    model_identity,
)
from self_heal.execution import RunExecutor
from self_heal.model import ChatModel
from self_heal.settings import AnalystConfig
from self_heal.storage import HistoryStore, HistoryError
from self_heal.table_store import TableStore, DatasetInfo
from self_heal.runner import CandidateRunner


def _verified_dataset(runner: CandidateRunner, dataset: Any) -> Any:
    """Read a dataset through the runner when it supports non-SQLite bundles.

    The original evaluator only supported SQLite datasets, so its lightweight
    test runners expose ``store.dataset_info`` rather than a runner method.
    Keep that contract while allowing the production runner to verify the
    logistics bundle that was actually passed to the isolated candidate.
    """
    lookup = getattr(runner, "dataset_info", None)
    if callable(lookup):
        return lookup(dataset)
    return runner.store.dataset_info(dataset.dataset_id)


@dataclass(frozen=True)
class EvaluationTrial:
    scenario_id: str
    dataset_id: str
    dataset_hash: str
    dataset_row_count: int
    scenario_seed: int | None
    task: dict[str, Any]
    expected_answer: dict[str, Any]
    answer: dict[str, Any] | None
    outcome: str
    error: str | None
    passed: bool
    violation: str | None
    model_calls: int
    tool_calls: int
    total_tokens: int
    elapsed_seconds: float
    table_pages: int
    table_bytes: int
    run_id: str = ""
    case_id: str | None = None
    trace_id: str | None = None
    trace_url: str | None = None
    trace_status: str = "not_traced"
    history_status: str = "not_recorded"
    trial_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvaluationRunner:
    """Runs fixed cases through a fresh scoped SQLite table session and retains every trial."""

    def __init__(
        self,
        store: TableStore,
        config: AnalystConfig,
        *,
        executor: RunExecutor | None = None,
        history: HistoryStore | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.executor = executor
        self.history = history
        self.trials: list[EvaluationTrial] = []

    def run_case(self, case: PreparedCase, model: ChatModel) -> EvaluationTrial:
        materialized = materialize_case(self.store, case)
        case_id = stable_case_id(
            scenario_id=case.scenario.scenario_id,
            dataset=materialized.dataset,
            task=case.scenario.task,
            oracle_version=self.config.evaluation.oracle_version,
        )
        history_status = "not_recorded"
        if self.history is not None:
            try:
                self.history.record_eval_case(
                    build_eval_case_record(
                        case_id=case_id,
                        scenario_id=case.scenario.scenario_id,
                        dataset=materialized.dataset,
                        task=case.scenario.task,
                        expected_answer=case.expected_answer,
                        oracle_version=self.config.evaluation.oracle_version,
                        config=self.config,
                        exposure_role="baseline",
                        created_at=utc_now(),
                    )
                )
                history_status = "recording"
            except (HistoryError, OSError):
                history_status = "incomplete"
        table = self.store.open_session(materialized.dataset.dataset_id)
        tools = AnalystTools(table, self.config)
        invocation: dict[str, Any] | str = case.scenario.question or case.scenario.task
        trace = None
        if self.executor is not None:
            execution = self.executor.run(
                model=model,
                tools=tools,
                dataset=materialized.dataset,
                invocation=invocation,
                case_id=case_id,
                case_exposure="baseline",
            )
            result = execution.result
            trace = execution.trace
            history_status = (
                execution.history_status
                if history_status != "incomplete"
                else "incomplete"
            )
        else:
            result = AnalystAgent(model, tools, self.config).run(invocation)
        trial_id = new_trial_id()
        trial = assess_run(
            materialized,
            result,
            self.config,
            case_id=case_id,
            trace_id=trace.trace_id if trace is not None else None,
            trace_url=trace.url if trace is not None else None,
            trace_status=trace.status if trace is not None else "not_traced",
            history_status=history_status,
            trial_id=trial_id,
        )
        if self.history is not None:
            try:
                self.history.record_evaluation(
                    build_evaluation_record(
                        evaluation_id=trial_id,
                        case_id=case_id,
                        result=result,
                        passed=trial.passed,
                        violation=trial.violation,
                        dataset=materialized.dataset,
                        trace=trace,
                        config=self.config,
                        created_at=utc_now(),
                    )
                )
                if history_status == "recording":
                    trial = replace(trial, history_status="recorded")
            except (HistoryError, OSError):
                trial = replace(trial, history_status="incomplete")
        self.trials.append(trial)
        return trial

    def run_cases(
        self,
        cases: Iterable[PreparedCase],
        model_factory: Callable[[], ChatModel],
    ) -> tuple[EvaluationTrial, ...]:
        return tuple(self.run_case(case, model_factory()) for case in cases)


def assess_run(
    materialized: MaterializedCase,
    result: RunResult,
    config: AnalystConfig,
    *,
    case_id: str | None = None,
    trace_id: str | None = None,
    trace_url: str | None = None,
    trace_status: str = "not_traced",
    history_status: str = "not_recorded",
    trial_id: str | None = None,
) -> EvaluationTrial:
    """Grade a harness result outside the editable harness boundary."""
    violation = _resource_violation(result, config)
    if violation is None:
        if result.outcome == "unsupported":
            violation = "capability_refusal_for_answerable_case"
        elif result.outcome == "error":
            violation = _error_violation(result.error)
        elif result.answer is None:
            violation = "missing_answer"
        elif result.answer != materialized.case.expected_answer:
            violation = "wrong_answer"
    return EvaluationTrial(
        scenario_id=materialized.case.scenario.scenario_id,
        dataset_id=materialized.dataset.dataset_id,
        dataset_hash=materialized.dataset.content_hash,
        dataset_row_count=materialized.dataset.row_count,
        scenario_seed=materialized.case.scenario.seed,
        task=dict(materialized.case.scenario.task),
        expected_answer=dict(materialized.case.expected_answer),
        answer=dict(result.answer) if result.answer is not None else None,
        outcome=result.outcome,
        error=result.error,
        passed=violation is None,
        violation=violation,
        model_calls=result.model_calls,
        tool_calls=result.tool_calls,
        total_tokens=result.total_tokens,
        elapsed_seconds=result.elapsed_seconds,
        table_pages=result.table_pages,
        table_bytes=result.table_bytes,
        run_id=result.run_id,
        case_id=case_id,
        trace_id=trace_id,
        trace_url=trace_url,
        trace_status=trace_status,
        history_status=history_status,
        trial_id=trial_id,
    )


def baseline_expectation_matches(trial: EvaluationTrial, expectation: str) -> bool:
    if expectation == "pass":
        return trial.passed
    if expectation == "fails_model_call_budget":
        return trial.violation == "model_call_budget_exhausted"
    raise ValueError("Scenario does not describe an answerable baseline expectation")


def _resource_violation(result: RunResult, config: AnalystConfig) -> str | None:
    limits = config.limits
    if result.model_calls > limits.max_model_calls:
        return "model_call_limit_exceeded"
    if result.tool_calls > limits.max_tool_calls:
        return "tool_call_limit_exceeded"
    if result.total_tokens > limits.max_total_tokens:
        return "token_limit_exceeded"
    if result.elapsed_seconds > limits.max_elapsed_seconds:
        return "time_limit_exceeded"
    if result.table_pages > limits.max_pages:
        return "table_page_limit_exceeded"
    if result.table_bytes > limits.max_bytes:
        return "table_byte_limit_exceeded"
    return None


def _error_violation(error: str | None) -> str:
    if not error:
        return "agent_error"
    normalized = error.lower()
    if "model-call budget" in normalized:
        return "model_call_budget_exhausted"
    if "tool-call budget" in normalized:
        return "tool_call_budget_exhausted"
    if "token budget" in normalized:
        return "token_budget_exhausted"
    if "time budget" in normalized:
        return "time_budget_exhausted"
    if "table page budget" in normalized:
        return "table_page_budget_exhausted"
    if "table byte budget" in normalized:
        return "table_byte_budget_exhausted"
    if "required table rows" in normalized:
        return "incomplete_table_read"
    return "agent_error"


def baseline_failure_reproduced(
    *,
    outcome: str,
    error: str | None,
    violation: str | None,
    observed_violation: str | None,
) -> bool:
    """Reproduce the unmet task contract, allowing stochastic diagnostic variation.

    An oracle-passing run never reproduces a failure. Provider, supervisor, and
    sandbox failures cannot establish a harness limitation. Capability gaps
    continue to require an explicit unsupported result.
    """
    if observed_violation is None or violation is None:
        return False
    text = (error or "").lower()
    if any(
        marker in text
        for marker in (
            "model or runtime failure",
            "isolated candidate failure",
            "supervisor execution failure",
            "cleanup failed",
        )
    ):
        return False
    if observed_violation == "capability_refusal_for_answerable_case":
        return outcome == "unsupported"
    return outcome in {"answered", "error"}


@dataclass(frozen=True)
class FrozenCase:
    case_id: str
    scenario_id: str
    dataset: DatasetInfo
    invocation: dict[str, Any] | str
    task: dict[str, Any] | None
    expected_answer: dict[str, Any] | None
    role: str
    origin: str

    def plan_record(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dataset_id": self.dataset.dataset_id,
            "dataset_hash": self.dataset.content_hash,
            "row_count": self.dataset.row_count,
            "invocation_hash": canonical_hash(self.invocation),
            "expected_answer_hash": canonical_hash(self.expected_answer),
            "role": self.role,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class SelectionDecision:
    accepted: bool
    reasons: tuple[str, ...]
    plan_id: str
    environment_hash: str
    candidate_commit: str
    parent_commit: str
    trials: tuple[dict[str, Any], ...]

    def summary(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "plan_id": self.plan_id,
            "environment_hash": self.environment_hash,
            "candidate_commit": self.candidate_commit,
            "parent_commit": self.parent_commit,
            "trials": list(self.trials),
        }


class SelectionEvaluator:
    """Freeze the comparison first, then run both commits through one Daytona boundary."""

    def __init__(
        self, runner: CandidateRunner, history: HistoryStore, config: AnalystConfig
    ):
        self.runner, self.history, self.config = runner, history, config

    def freeze_case(
        self,
        *,
        scenario_id: str,
        dataset: DatasetInfo,
        invocation: dict[str, Any] | str,
        task: dict[str, Any] | None,
        role: str,
        origin: str,
    ) -> FrozenCase:
        if dataset.dataset_id.startswith("final-"):
            raise HistoryError(
                "Final assessment datasets cannot enter candidate selection"
            )
        verified = self.runner.store.dataset_info(dataset.dataset_id)
        if verified != dataset:
            raise HistoryError("Dataset changed before case freeze")
        expected = (
            reference_answer(
                self.runner.store.verified_rows(dataset.dataset_id), task, self.config
            )
            if task
            else None
        )
        case_id = stable_case_id(
            scenario_id=scenario_id,
            dataset=dataset,
            task=task or {"unsupported": invocation},
            oracle_version=self.config.evaluation.oracle_version,
        )
        case = FrozenCase(
            case_id, scenario_id, dataset, invocation, task, expected, role, origin
        )
        existing = self.history.get_eval_case(case_id)
        if existing:
            if existing["dataset"]["content_hash"] != dataset.content_hash or existing[
                "oracle"
            ]["expected_answer_hash"] != canonical_hash(expected):
                raise HistoryError("Frozen case identity conflicts with stored oracle")
            self.history.add_case_exposure(
                case_id, role=role, source="selection", at=utc_now()
            )
        else:
            self.history.record_eval_case(
                {
                    "_id": case_id,
                    "case_id": case_id,
                    "schema_version": 1,
                    "scenario_id": scenario_id,
                    "task": task,
                    "invocation": invocation,
                    "task_family": self.config.task_family,
                    "task_contract_version": self.config.task_contract_version,
                    "dataset": {
                        "id": dataset.dataset_id,
                        "content_hash": dataset.content_hash,
                        "row_count": dataset.row_count,
                    },
                    "oracle": {
                        "version": self.config.evaluation.oracle_version,
                        "expected_answer_hash": canonical_hash(expected),
                    },
                    "origin": origin,
                    "created_at": utc_now(),
                    "exposure": {"role": role, "source": "selection", "at": utc_now()},
                }
            )
        return case

    def declared_regressions(self) -> list[FrozenCase]:
        scenarios = load_scenarios(self.config.evaluation.scenarios_path, self.config)
        cases = []
        for scenario in scenarios.values():
            if scenario.baseline_expectation != "pass":
                continue
            prepared = prepare_case(scenario, self.config)
            dataset = materialize_case(self.runner.store, prepared).dataset
            cases.append(
                self.freeze_case(
                    scenario_id=scenario.scenario_id,
                    dataset=dataset,
                    invocation=scenario.question or scenario.task,
                    task=scenario.task,
                    role="regression",
                    origin="declared_scenario",
                )
            )
        for stored in self.history.eval_cases.find(
            {"origin": "observed_incident", "exposures.role": "regression",
             "dataset.input_kind": {"$ne": "logistics_bundle"}}
        ):
            if not stored.get("invocation") or not stored.get("task"):
                continue
            dataset = self.runner.store.dataset_info(stored["dataset"]["id"])
            cases.append(
                self.freeze_case(
                    scenario_id=stored["scenario_id"],
                    dataset=dataset,
                    invocation=stored["invocation"],
                    task=stored["task"],
                    role="regression",
                    origin="observed_incident",
                )
            )
        return cases

    def private_validation_cases(
        self, original_task: dict[str, Any]
    ) -> list[FrozenCase]:
        """Fresh data and question variants are created before the candidate is proposed."""
        cases = []
        for index in range(self.config.evaluation.validation_cases):
            seed = secrets.randbits(31)
            nonce = secrets.token_hex(8)
            task = dict(original_task)
            if task.get("filter_field") == "warehouse":
                task["filter_value"] = "Cedar"
            elif task.get("filter_field") == "category":
                task["filter_value"] = "Tools"
            if index % 4 == 1:
                task = {"metric": original_task["metric"], "group_by": "category"}
            elif index % 4 == 2:
                task = {
                    "metric": original_task["metric"],
                    "filter_field": "category",
                    "filter_value": "Tools",
                    "group_by": "warehouse",
                }
            elif index % 4 == 3:
                task = {
                    "metric": original_task["metric"],
                    "filter_field": "sku",
                    "filter_value": "SKU-NOT-PRESENT",
                    "group_by": "category",
                }
            count = (97, 257, 384, 31)[index % 4]
            scenario = Scenario(
                scenario_id=f"private-{nonce}",
                dataset_id=f"private-{nonce}",
                description="Independent selection variation",
                task=task,
                question=_question_for(task),
                baseline_expectation="pass",
                fixture=None,
                seed=seed,
                row_count=count,
                warehouses=("Cedar", "Harbor", "Quartz"),
                categories=("Tools", "Supply", "Parts"),
                on_hand_min=1,
                on_hand_max=97,
                invalid_row=None,
            )
            prepared = prepare_case(scenario, self.config)
            dataset = materialize_case(self.runner.store, prepared).dataset
            cases.append(
                self.freeze_case(
                    scenario_id=scenario.scenario_id,
                    dataset=dataset,
                    invocation=scenario.question or task,
                    task=task,
                    role="private_validation",
                    origin="independent_generation",
                )
            )
        return cases

    def negative_refusal(self, dataset: DatasetInfo) -> FrozenCase:
        return self.freeze_case(
            scenario_id="unsupported-revenue",
            dataset=dataset,
            invocation="What is the total revenue in this table?",
            task=None,
            role="negative_refusal",
            origin="protected_contract_check",
        )

    def select(
        self,
        *,
        candidate_id: str,
        parent_commit: str,
        candidate_commit: str,
        parent_source: Path,
        candidate_source: Path,
        original: FrozenCase,
        regressions: list[FrozenCase],
        validation: list[FrozenCase],
        negative: FrozenCase,
        model_factory: Callable[[], ChatModel],
        observed_violation: str | None,
        generated: FrozenCase | None = None,
    ) -> SelectionDecision:
        if not validation or len(validation) < self.config.evaluation.validation_cases:
            raise ValueError("Fresh private validation is incomplete")
        cases = [
            original,
            *([generated] if generated else []),
            *regressions,
            *validation,
            negative,
        ]
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("Selection cases must be distinct")
        image = self.runner.image_identity()
        candidate_record = self.history.candidates.find_one({"_id": candidate_id}) or {}
        workflow_identity = {
            "parent_workflow_revision_id": candidate_record.get(
                "parent_workflow_revision_id"
            ),
            "candidate_workflow_revision_id": candidate_record.get(
                "workflow_revision_id"
            ),
        }
        identity = {
            "candidate_commit": candidate_commit,
            "parent_commit": parent_commit,
            "config_hash": self.config.config_hash,
            "image": image,
            "model": model_identity(model_factory()),
            "workflows": workflow_identity,
        }
        environment_hash = canonical_hash(identity)
        plan_id = "plan_" + secrets.token_hex(16)
        self.history.record_selection_plan(
            {
                "_id": plan_id,
                "candidate_id": candidate_id,
                "candidate_commit": candidate_commit,
                "parent_commit": parent_commit,
                "config_hash": self.config.config_hash,
                "environment_hash": environment_hash,
                "identity": identity,
                "cases": [case.plan_record() for case in cases],
                **workflow_identity,
                "created_at": utc_now(),
            }
        )
        trials: list[dict[str, Any]] = []
        reasons: set[str] = set()
        for case in cases:
            if self.runner.image_identity() != image:
                reasons.add("environment_identity_changed")
                break
            before = _verified_dataset(self.runner, case.dataset)
            if before != case.dataset:
                reasons.add("dataset_identity_changed")
                break
            for version, source, commit in (
                ("baseline", parent_source, parent_commit),
                ("candidate", candidate_source, candidate_commit),
            ):
                repetitions = (
                    self.config.evaluation.live_repetitions
                    if case.role in {"original", "private_validation"}
                    else 1
                )
                for repeat in range(repetitions):
                    if self.runner.image_identity() != image:
                        reasons.add("environment_identity_changed")
                        break
                    model = model_factory()
                    if model_identity(model) != identity["model"]:
                        reasons.add("model_identity_changed")
                    execution = self.runner.run(
                        source=source,
                        source_commit=commit,
                        dataset=case.dataset,
                        invocation=case.invocation,
                        model=model,
                        case_id=case.case_id,
                        case_exposure=case.role,
                    )
                    result = execution.result
                    violation = _selection_violation(case, result, self.config)
                    record = build_evaluation_record(
                        evaluation_id=new_trial_id(),
                        case_id=case.case_id,
                        result=result,
                        passed=violation is None,
                        violation=violation,
                        dataset=case.dataset,
                        trace=execution.trace,
                        config=self.config,
                        created_at=utc_now(),
                    )
                    record.update(
                        {
                            "candidate": {
                                "candidate_id": candidate_id,
                                "commit": commit,
                                "version": version,
                            },
                            "plan_id": plan_id,
                            "environment_hash": environment_hash,
                            "case_role": case.role,
                            "repeat": repeat,
                            "workflow_revision_id": (
                                workflow_identity["parent_workflow_revision_id"]
                                if version == "baseline"
                                else workflow_identity["candidate_workflow_revision_id"]
                            ),
                        }
                    )
                    self.history.record_evaluation(record)
                    trials.append(
                        {
                            "trial_id": record["trial_id"],
                            "case_id": case.case_id,
                            "role": case.role,
                            "version": version,
                            "repeat": repeat,
                            "passed": violation is None,
                            "violation": violation,
                            "outcome": result.outcome,
                            "error": result.error,
                            "model_calls": result.model_calls,
                            "tool_calls": result.tool_calls,
                            "total_tokens": result.total_tokens,
                            "table_pages": result.table_pages,
                            "table_bytes": result.table_bytes,
                            "elapsed_seconds": result.elapsed_seconds,
                            "trace_id": execution.trace.trace_id,
                            "trace_status": execution.trace.status,
                            "history_status": execution.history_status,
                            "dataset_hash": case.dataset.content_hash,
                        }
                    )
                    if execution.history_status != "recorded":
                        reasons.add("incomplete_history")
                    if (
                        self.config.evaluation.require_trace
                        and execution.trace.status != "available"
                    ):
                        reasons.add("incomplete_trace")
                    if _verified_dataset(self.runner, case.dataset) != case.dataset:
                        reasons.add("dataset_identity_changed")
                        break

        def for_case(case, version):
            return [
                trial
                for trial in trials
                if trial["case_id"] == case.case_id and trial["version"] == version
            ]

        original_base = for_case(original, "baseline")
        original_new = for_case(original, "candidate")
        if len(original_base) != self.config.evaluation.live_repetitions or not all(
            baseline_failure_reproduced(
                outcome=trial["outcome"],
                error=trial.get("error"),
                violation=trial["violation"],
                observed_violation=observed_violation,
            )
            for trial in original_base
        ):
            reasons.add("baseline_failure_not_reproduced")
        if len(original_new) != self.config.evaluation.live_repetitions or not all(
            t["passed"] for t in original_new
        ):
            reasons.add("original_case_failed")
        if generated is not None:
            old, new = for_case(generated, "baseline"), for_case(generated, "candidate")
            if len(old) != 1 or not baseline_failure_reproduced(
                outcome=old[0]["outcome"],
                error=old[0].get("error"),
                violation=old[0]["violation"],
                observed_violation=observed_violation,
            ):
                reasons.add("generated_failure_not_reproduced")
            if len(new) != 1 or not new[0]["passed"]:
                reasons.add("generated_case_failed")
        for case in regressions:
            old, new = for_case(case, "baseline"), for_case(case, "candidate")
            if len(old) != 1 or not old[0]["passed"]:
                reasons.add("regression_baseline_unstable")
            if len(new) != 1 or not new[0]["passed"]:
                reasons.add("regression_failed")
        for case in validation:
            new = for_case(case, "candidate")
            if len(new) != self.config.evaluation.live_repetitions or not all(
                t["passed"] for t in new
            ):
                reasons.add("validation_failed_or_flaky")
        negative_new = for_case(negative, "candidate")
        if len(negative_new) != 1 or not negative_new[0]["passed"]:
            reasons.add("unrelated_refusal_lost")
        old_cost = sum(
            t["total_tokens"]
            for t in trials
            if t["version"] == "baseline" and t["role"] == "regression"
        )
        new_cost = sum(
            t["total_tokens"]
            for t in trials
            if t["version"] == "candidate" and t["role"] == "regression"
        )
        if old_cost and new_cost > old_cost * self.config.evaluation.max_cost_ratio:
            reasons.add("regression_cost_exceeded")
        return SelectionDecision(
            accepted=not reasons,
            reasons=tuple(sorted(reasons)),
            plan_id=plan_id,
            environment_hash=environment_hash,
            candidate_commit=candidate_commit,
            parent_commit=parent_commit,
            trials=tuple(trials),
        )


def _selection_violation(
    case: FrozenCase, result: RunResult, config: AnalystConfig
) -> str | None:
    resource = _resource_violation(result, config)
    if resource:
        return resource
    if case.role == "negative_refusal":
        return (
            None
            if result.outcome == "unsupported"
            and result.limitation_kind == "capability_gap"
            else "incorrect_refusal"
        )
    if result.outcome == "unsupported":
        return "capability_refusal_for_answerable_case"
    if result.outcome == "error":
        return _error_violation(result.error)
    if _without_nulls(result.interpreted_task) != _without_nulls(case.task):
        return "task_mismatch"
    if result.answer != case.expected_answer:
        return "wrong_answer"
    return None


def _without_nulls(task: dict[str, Any] | None) -> dict[str, Any] | None:
    """Treat an explicit null field the same as an omitted one when comparing tasks."""

    if task is None:
        return None
    return {key: value for key, value in task.items() if value is not None}


def _question_for(task: dict[str, Any]) -> str:
    metric = {"available": "available", "on_hand": "on-hand", "reserved": "reserved"}[
        task["metric"]
    ]
    if task.get("group_by"):
        question = f"What are the {metric} units in each {task['group_by']}?"
    else:
        question = f"How many {metric} units are there"
    if task.get("filter_field"):
        question = (
            question.rstrip("?")
            + f" for {task['filter_field']} {task['filter_value']}?"
        )
    elif not question.endswith("?"):
        question += "?"
    return question
