"""Protected automatic evolution for the logistics threshold harness."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Callable

from evals.logistics.generator import private_evaluation_bundle
from evals.logistics.oracle import ORACLE_VERSION, LogisticsOracleError, reference_answer
from harness.agent import logistics_capability_request
from self_heal.contracts import (
    build_evaluation_record, canonical_hash, model_identity, new_candidate_id,
    new_trial_id, utc_now,
)
from self_heal.controller import EvolutionBlocked
from self_heal.evaluation import FrozenCase, SelectionEvaluator, _selection_violation
from self_heal.evolution import ProposalError, propose_logistics_change
from self_heal.logistics_store import DatasetBundleInfo, LogisticsDatasetStore
from self_heal.model import ChatModel
from self_heal.promotion import PromotionManager, PromotionRejected
from self_heal.repository import CandidateRepository, PatchRejected
from self_heal.runner import CandidateRunner, RunnerError
from self_heal.settings import AnalystConfig
from self_heal.storage import HistoryStore, HistoryError
from self_heal.table_store import DatasetError
from self_heal.telemetry import PhoenixTelemetry


class LogisticsEvolutionController:
    """Evolve only the registered, bounded logistics threshold contract.

    This controller deliberately owns its own incident and fresh-bundle setup.
    Inventory's scenario generator and oracle cannot grade a related-data
    bundle, while the shared selection evaluator still supplies frozen paired
    trials, resource gates, persistence, and promotion checks.
    """

    def __init__(
        self, *, logistics: LogisticsDatasetStore, history: HistoryStore,
        config: AnalystConfig, telemetry: PhoenixTelemetry,
        repository: CandidateRepository, runner: CandidateRunner,
        model_factory: Callable[[], ChatModel], evolution_model: ChatModel,
        on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.logistics, self.history, self.config, self.telemetry = logistics, history, config, telemetry
        self.repository, self.runner = repository, runner
        self.model_factory, self.evolution_model, self.on_progress = model_factory, evolution_model, on_progress
        if hasattr(telemetry, "observe_model"):
            self.evolution_model = telemetry.observe_model(evolution_model)
        self.evaluator = SelectionEvaluator(runner, history, config)

    def _progress(self, stage: str, **payload: Any) -> None:
        if self.on_progress is not None:
            self.on_progress(stage, payload)

    def evolve(self, run_id: str) -> dict[str, Any]:
        observed = self.history.get_run(run_id)
        if not observed or observed.get("status") != "completed":
            raise EvolutionBlocked("Observed run is unavailable or incomplete")
        invocation = (observed.get("invocation") or {}).get("question")
        if not isinstance(invocation, str):
            raise EvolutionBlocked("Original logistics question is unavailable")
        task = _task_for_question(invocation)
        if task is None:
            raise EvolutionBlocked("Question is outside the protected logistics contract")
        if observed.get("outcome") != "unsupported" or observed.get("limitation_kind") != "capability_gap":
            raise EvolutionBlocked("Observed logistics run is not an explicit capability gap")
        if observed.get("execution", {}).get("source", {}).get("dirty"):
            raise EvolutionBlocked("Observed source was dirty and cannot be replayed exactly")
        dataset_record = observed.get("dataset") or {}
        if dataset_record.get("input_kind") != "logistics_bundle":
            raise EvolutionBlocked("Observed run did not use a logistics bundle")
        dataset = self.logistics.dataset_info(dataset_record.get("id", ""))
        if dataset.content_hash != dataset_record.get("content_hash"):
            raise EvolutionBlocked("Observed logistics bundle changed")
        baseline_commit = self.repository.resolve_commit(
            observed.get("execution", {}).get("source", {}).get("commit")
        )
        existing = self.history.get_gap(run_id)
        if existing and existing.get("status") == "resolved":
            raise EvolutionBlocked("Incident is already resolved")
        attempts = self.history.candidates.count_documents({
            "incident_run_id": run_id, "rescreen_of": {"$exists": False},
        })
        if attempts >= self.config.evaluation.max_patch_attempts:
            raise EvolutionBlocked("Patch attempt limit reached for this incident")
        if not existing:
            self.history.record_gap({
                "_id": run_id, "run_id": run_id, "task_family": self.config.task_family,
                "status": "observed", "reason": observed.get("limitation_reason") or "Capability gap",
                "question": invocation, "baseline_commit": baseline_commit, "dataset": dataset_record,
                "trace_id": (observed.get("trace") or {}).get("root_id"), "created_at": utc_now(),
            })
        self._progress("incident_classified", run_id=run_id, baseline_commit=baseline_commit)
        try:
            return self._evolve(run_id, observed, invocation, task, dataset, baseline_commit, attempts + 1)
        except (EvolutionBlocked, LogisticsOracleError, ProposalError, PatchRejected, RunnerError, HistoryError,
                DatasetError) as exc:
            self.history.update_gap(run_id, status="open", reason=str(exc))
            return {"run_id": run_id, "status": "open", "reason": str(exc)}

    def _evolve(
        self, run_id: str, observed: dict[str, Any], question: str, task: dict[str, Any],
        dataset: DatasetBundleInfo, baseline_commit: str, first_attempt: int,
    ) -> dict[str, Any]:
        trace_record = observed.get("trace") or {}
        trace = (self.telemetry.read_redacted_trace(trace_record["root_id"])
                 if trace_record.get("status") == "available" and trace_record.get("root_id") else [])
        self._progress("trace_read", available=bool(trace))
        original = self._freeze(
            scenario_id="observed-" + run_id.replace("-", "")[:24] + "-original",
            dataset=dataset, invocation=question, task=task, role="original", origin="observed_incident",
        )
        generated_bundle = private_evaluation_bundle(secrets.randbits(31))
        generated_dataset = self.logistics.materialize(
            "incident-logistics-" + secrets.token_hex(8), **generated_bundle,
        )
        generated_task = {**task, "threshold": max(0, task["threshold"] - 1)}
        generated = self._freeze(
            scenario_id="generated-" + run_id.replace("-", "")[:24], dataset=generated_dataset,
            invocation=_question_for(generated_task), task=generated_task,
            role="generated_reproduction", origin="independent_generation",
        )
        validation = self._private_validation_cases(task)
        negative = self._freeze(
            scenario_id="negative-logistics-" + run_id.replace("-", "")[:24], dataset=dataset,
            invocation="How many customers sent more than 15 shipments from warehouse 3 today?", task=None,
            role="negative_refusal", origin="protected_contract_check",
        )
        regressions = self.evaluator.declared_regressions()
        self._progress("cases_frozen", original_case_id=original.case_id,
                       generated_case_id=generated.case_id,
                       validation_case_count=len(validation), regression_case_count=len(regressions))
        baseline_source = self.repository.create_worktree(baseline_commit)
        for case in (original, generated):
            execution = self.runner.run(
                source=baseline_source, source_commit=baseline_commit, dataset=case.dataset,
                invocation=case.invocation, model=self.model_factory(), case_id=case.case_id,
                case_exposure="reproduction",
            )
            violation = _selection_violation(case, execution.result, self.config)
            self._record_probe(case, execution, violation, baseline_commit)
            if execution.result.outcome != "unsupported":
                raise EvolutionBlocked(f"Baseline failure did not reproduce on {case.role}")
        self._progress("baseline_reproduced", original_case_id=original.case_id,
                       generated_case_id=generated.case_id)
        source = {path.name: path.read_text() for path in sorted((baseline_source / "harness").glob("*.py"))}
        incident = {
            "question": question, "outcome": observed.get("outcome"),
            "limitation_kind": observed.get("limitation_kind"),
            "limitation_reason": observed.get("limitation_reason"),
            "task_family": self.config.task_family, "required_tool": "count_customers_over_shipment_threshold",
        }
        contract = {
            "version": self.config.task_contract_version,
            "operation": task["operation"], "input_kind": dataset.input_kind,
            "relations": sorted(dataset.relations), "limits": vars(self.config.limits),
        }
        prior = [{"hypothesis": row.get("hypothesis"), "status": row.get("status"),
                  "reason": (row.get("selection") or {}).get("reasons", row.get("rejection_reason"))}
                 for row in self.history.candidates_for(task_family=self.config.task_family)]
        for attempt in range(first_attempt, self.config.evaluation.max_patch_attempts + 1):
            candidate_id = new_candidate_id()
            proposal = None
            try:
                proposal = propose_logistics_change(
                    self.evolution_model, incident=incident, trace=trace, contract=contract, source=source,
                    reproduction={"original_violation": "capability_refusal_for_answerable_case",
                                  "generated_case_id": generated.case_id}, previous_attempts=prior,
                )
                self._progress("proposal_received", candidate_id=candidate_id,
                               changed_mechanism=proposal.changed_mechanism,
                               hypothesis=proposal.hypothesis, diff=proposal.diff)
                candidate_source = self.repository.apply_proposal(baseline_commit, proposal.diff)
                self.repository.inspect(candidate_source)
            except (ProposalError, PatchRejected) as exc:
                self.history.record_candidate({
                    "_id": candidate_id, "candidate_id": candidate_id, "incident_run_id": run_id,
                    "task_family": self.config.task_family,
                    "changed_mechanism": proposal.changed_mechanism if proposal else "rejected_proposal",
                    "hypothesis": proposal.hypothesis if proposal else "Proposal failed screening",
                    "diff": proposal.diff if proposal else None, "attempt": attempt,
                    "parent_commit": baseline_commit, "rejection_reason": str(exc),
                    "status": "rejected", "created_at": utc_now(),
                })
                prior.append({"hypothesis": proposal.hypothesis if proposal else "Proposal failed screening",
                              "status": "rejected", "reason": str(exc)})
                self._progress("proposal_rejected", candidate_id=candidate_id, reason=str(exc))
                continue
            self.history.record_candidate({
                "_id": candidate_id, "candidate_id": candidate_id, "incident_run_id": run_id,
                "candidate_commit": candidate_source.candidate_commit, "parent_commit": baseline_commit,
                "task_family": self.config.task_family, "changed_mechanism": proposal.changed_mechanism,
                "hypothesis": proposal.hypothesis, "diff": proposal.diff,
                "changed_paths": candidate_source.changed_paths, "attempt": attempt, "created_at": utc_now(),
            })
            self._progress("candidate_ready", candidate_id=candidate_id,
                           candidate_commit=candidate_source.candidate_commit,
                           parent_commit=baseline_commit, candidate_source=str(candidate_source.worktree))
            result = self._evaluate(
                run_id=run_id, candidate_id=candidate_id, baseline_commit=baseline_commit,
                baseline_source=baseline_source, candidate_source=candidate_source,
                original=original, generated=generated, regressions=regressions,
                validation=validation, negative=negative,
            )
            if result["status"] == "activated":
                return result
            prior.append({"hypothesis": proposal.hypothesis, "status": "rejected",
                          "reason": result["selection"]["reasons"]})
        self.history.update_gap(run_id, status="open", reason="Patch attempt limit reached")
        return {"run_id": run_id, "status": "open", "reason": "Patch attempt limit reached"}

    def _evaluate(self, *, run_id: str, candidate_id: str, baseline_commit: str, baseline_source: Path,
                  candidate_source: Any, original: FrozenCase, generated: FrozenCase,
                  regressions: list[FrozenCase], validation: list[FrozenCase], negative: FrozenCase) -> dict[str, Any]:
        try:
            self._progress("evaluating", candidate_id=candidate_id,
                           candidate_commit=candidate_source.candidate_commit)
            decision = self.evaluator.select(
                candidate_id=candidate_id, parent_commit=baseline_commit,
                candidate_commit=candidate_source.candidate_commit, parent_source=baseline_source,
                candidate_source=candidate_source.worktree, original=original, generated=generated,
                regressions=regressions, validation=validation, negative=negative,
                model_factory=self.model_factory, observed_violation="capability_refusal_for_answerable_case",
            )
        except (RunnerError, PatchRejected, ValueError) as exc:
            summary = {"accepted": False, "reasons": [f"Candidate evaluation could not complete: {type(exc).__name__}"]}
            self.history.record_candidate_result(candidate_id, summary)
            self.history.append_candidate_transition(candidate_id, status="rejected", at=utc_now(), reason=summary["reasons"][0])
            return {"run_id": run_id, "status": "open", "candidate_id": candidate_id, "selection": summary}
        summary = decision.summary()
        self.history.record_candidate_result(candidate_id, summary)
        self._progress("selection_decided", candidate_id=candidate_id, plan_id=decision.plan_id,
                       accepted=decision.accepted, reasons=list(decision.reasons))
        if not decision.accepted:
            self.history.append_candidate_transition(candidate_id, status="rejected", at=utc_now(),
                                                     reason=", ".join(decision.reasons))
            return {"run_id": run_id, "status": "open", "candidate_id": candidate_id,
                    "candidate_commit": candidate_source.candidate_commit, "selection": summary}
        candidate = self.history.candidates.find_one({"_id": candidate_id}) or {}
        environment_hash = canonical_hash({
            "candidate_commit": candidate_source.candidate_commit, "parent_commit": baseline_commit,
            "config_hash": self.config.config_hash, "image": self.runner.image_identity(),
            "model": model_identity(self.model_factory()), "workflows": {
                "parent_workflow_revision_id": candidate.get("parent_workflow_revision_id"),
                "candidate_workflow_revision_id": candidate.get("workflow_revision_id"),
            },
        })
        try:
            promotion = PromotionManager(self.history, self.repository).activate(
                task_family=self.config.task_family, candidate_id=candidate_id,
                source=candidate_source, decision=decision, current_environment_hash=environment_hash,
            )
        except PromotionRejected as exc:
            self.history.append_candidate_transition(candidate_id, status="stale", at=utc_now(), reason=str(exc))
            return {"run_id": run_id, "status": "open", "candidate_id": candidate_id,
                    "candidate_commit": candidate_source.candidate_commit,
                    "selection": {**summary, "accepted": False, "reasons": [str(exc)]}}
        self.history.add_case_exposure(original.case_id, role="regression", source="accepted_candidate", at=utc_now())
        self.history.update_gap(run_id, status="resolved", reason="Validated candidate activated")
        self._progress("promotion_completed", candidate_id=candidate_id,
                       candidate_commit=candidate_source.candidate_commit,
                       plan_id=decision.plan_id, **promotion)
        return {"run_id": run_id, "status": "activated", "candidate_id": candidate_id,
                "candidate_commit": candidate_source.candidate_commit,
                "selection": summary, "promotion": promotion}

    def _freeze(self, *, scenario_id: str, dataset: DatasetBundleInfo, invocation: str,
                task: dict[str, Any] | None, role: str, origin: str) -> FrozenCase:
        verified = self.logistics.dataset_info(dataset.dataset_id)
        if verified != dataset:
            raise EvolutionBlocked("Frozen logistics bundle changed")
        expected = _oracle_bundle(self.logistics, dataset) if task is not None else None
        answer = reference_answer(expected, task) if task is not None else None
        case_id = "case_" + canonical_hash({
            "scenario_id": scenario_id, "dataset_id": dataset.dataset_id,
            "dataset_hash": dataset.content_hash, "task": task or {"unsupported": invocation},
            "oracle_version": ORACLE_VERSION,
        })[:32]
        current = self.history.get_eval_case(case_id)
        if current:
            if (current.get("dataset", {}).get("content_hash") != dataset.content_hash
                    or current.get("oracle", {}).get("expected_answer_hash") != canonical_hash(answer)):
                raise EvolutionBlocked("Frozen logistics case conflicts with stored oracle")
            self.history.add_case_exposure(case_id, role=role, source="selection", at=utc_now())
        else:
            self.history.record_eval_case({
                "_id": case_id, "case_id": case_id, "schema_version": 1, "scenario_id": scenario_id,
                "task": task, "invocation": invocation, "task_family": self.config.task_family,
                "task_contract_version": self.config.task_contract_version,
                "dataset": {"id": dataset.dataset_id, "content_hash": dataset.content_hash,
                            "row_count": dataset.row_count, "input_kind": dataset.input_kind},
                "oracle": {"version": ORACLE_VERSION, "expected_answer_hash": canonical_hash(answer)},
                "origin": origin, "created_at": utc_now(),
                "exposure": {"role": role, "source": "selection", "at": utc_now()},
            })
        return FrozenCase(case_id, scenario_id, dataset, invocation, task, answer, role, origin)

    def _private_validation_cases(self, original: dict[str, Any]) -> list[FrozenCase]:
        cases = []
        for index in range(self.config.evaluation.validation_cases):
            seed, nonce = secrets.randbits(31), secrets.token_hex(8)
            dataset = self.logistics.materialize("private-logistics-" + nonce, **private_evaluation_bundle(seed))
            task = {**original, "threshold": max(0, original["threshold"] + (index - 1))}
            cases.append(self._freeze(
                scenario_id="private-logistics-" + nonce, dataset=dataset,
                invocation=_question_for(task), task=task,
                role="private_validation", origin="independent_generation",
            ))
        return cases

    def _record_probe(self, case: FrozenCase, execution: Any, violation: str | None, baseline_commit: str) -> None:
        record = build_evaluation_record(
            evaluation_id=new_trial_id(), case_id=case.case_id, result=execution.result,
            passed=violation is None, violation=violation, dataset=case.dataset,
            trace=execution.trace, config=self.config, created_at=utc_now(),
        )
        record["candidate"] = {"commit": baseline_commit, "version": "baseline_reproduction"}
        self.history.record_evaluation(record)


class FamilyEvolutionController:
    """Choose a controller from the persisted incident contract, never UI state."""

    def __init__(
        self, *, history: HistoryStore, inventory: Callable[[], Any],
        logistics: Callable[[], LogisticsEvolutionController],
    ) -> None:
        self.history, self.inventory, self.logistics = history, inventory, logistics

    def evolve(self, run_id: str) -> dict[str, Any]:
        observed = self.history.get_run(run_id)
        family = (observed or {}).get("invocation", {}).get("task_family")
        if family == "logistics-shipment-threshold":
            return self.logistics().evolve(run_id)
        return self.inventory().evolve(run_id)


def _task_for_question(question: str) -> dict[str, Any] | None:
    request = logistics_capability_request(question)
    if request is None:
        return None
    return {key: request[key] for key in ("operation", "warehouse_number", "relative_day", "threshold")}


def _question_for(task: dict[str, Any]) -> str:
    return (f"How many customers sent more than {task['threshold']} shipments from warehouse "
            f"{task['warehouse_number']} yesterday?")


def _oracle_bundle(store: LogisticsDatasetStore, dataset: DatasetBundleInfo) -> dict[str, Any]:
    return {
        **store.verified_relations(dataset.dataset_id),
        "reference_instant": dataset.reference_instant.isoformat(),
        "reporting_timezone": dataset.reporting_timezone,
    }
