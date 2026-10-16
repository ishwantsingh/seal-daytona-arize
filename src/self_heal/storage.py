"""Compact, queryable supervisor history in the SQLite database.

Detailed model and tool payloads stay in Arize Phoenix. This store keeps only the
identities and measurements needed to reproduce, evaluate, and evolve the
harness in later phases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from self_heal.database import ASCENDING, DESCENDING, ReturnDocument
from self_heal.database import SQLiteDatabase as Database
from self_heal.database import DuplicateKeyError, DatabaseError


class HistoryError(RuntimeError):
    pass


class HistoryStore:
    def __init__(self, database: Database, *, evaluation_sink=None) -> None:
        self.database = database
        self.evaluation_sink = evaluation_sink
        self.runs = database["runs"]
        self.eval_cases = database["eval_cases"]
        self.candidates = database["candidates"]
        self.evaluations = database["evaluations"]
        self.versions = database["versions"]
        self.active_versions = database["active_versions"]
        self.gaps = database["capability_gaps"]
        self.selection_plans = database["selection_plans"]
        self.final_assessments = database["final_assessments"]
        self.harness_workflows = database["harness_workflows"]
        self.evolution_jobs = database["evolution_jobs"]
        self.evolution_events = database["evolution_events"]

    def ensure_indexes(self) -> None:
        """Create the Phase 3 query paths without a duplicate event collection."""

        self.runs.create_index([("run_id", ASCENDING)], unique=True)
        self.runs.create_index([("outcome", ASCENDING), ("limitation_kind", ASCENDING), ("created_at", DESCENDING)])
        self.runs.create_index([("invocation.task_family", ASCENDING), ("created_at", DESCENDING)])
        self.runs.create_index([("invocation.task_id", ASCENDING), ("created_at", DESCENDING)])
        self.runs.create_index([("dataset.id", ASCENDING), ("dataset.content_hash", ASCENDING)])
        self.runs.create_index([("execution.source.commit", ASCENDING), ("created_at", DESCENDING)])
        self.runs.create_index([("trace.root_id", ASCENDING)], unique=True, sparse=True)

        self.eval_cases.create_index([("case_id", ASCENDING)], unique=True)
        self.eval_cases.create_index([("task_family", ASCENDING), ("created_at", DESCENDING)])
        self.eval_cases.create_index([("dataset.id", ASCENDING), ("dataset.content_hash", ASCENDING)])
        self.eval_cases.create_index([("scenario_id", ASCENDING), ("created_at", DESCENDING)])

        self.candidates.create_index([("candidate_id", ASCENDING)], unique=True)
        self.candidates.create_index([("candidate_commit", ASCENDING)], unique=True, sparse=True)
        self.candidates.create_index([("rescreen_of", ASCENDING)], unique=True, sparse=True)
        self.candidates.create_index([("incident_run_id", ASCENDING), ("attempt", ASCENDING)])
        self.candidates.create_index(
            [("task_family", ASCENDING), ("changed_mechanism", ASCENDING), ("created_at", DESCENDING)]
        )

        self.evaluations.create_index([("evaluation_id", ASCENDING)], unique=True)
        self.evaluations.create_index([("trial_id", ASCENDING)], unique=True, sparse=True)
        self.evaluations.create_index([("run_id", ASCENDING), ("case_id", ASCENDING)], unique=True)
        self.evaluations.create_index([("case_id", ASCENDING), ("passed", ASCENDING), ("created_at", DESCENDING)])
        self.evaluations.create_index([("candidate.commit", ASCENDING), ("created_at", DESCENDING)])

        self.versions.create_index([("version_id", ASCENDING)], unique=True)
        self.versions.create_index([("identity_hash", ASCENDING)], unique=True, sparse=True)
        self.versions.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
        self.gaps.create_index([("run_id", ASCENDING)], unique=True)
        self.selection_plans.create_index([("candidate_id", ASCENDING)], unique=True)
        self.final_assessments.create_index([("case_id", ASCENDING)], unique=True)
        self.final_assessments.create_index([("dataset.id", ASCENDING)], unique=True)
        self.final_assessments.create_index([("commit", ASCENDING), ("started_at", DESCENDING)])
        self.harness_workflows.create_index([("workflow_revision_id", ASCENDING)], unique=True)
        self.harness_workflows.create_index([("identity_hash", ASCENDING)], unique=True)
        self.harness_workflows.create_index([("task_family", ASCENDING), ("created_at", DESCENDING)])
        self.harness_workflows.create_index([("source_commit", ASCENDING), ("configuration_hash", ASCENDING)])
        self.evolution_jobs.create_index([("incident_run_id", ASCENDING)], unique=True)
        self.evolution_jobs.create_index([("status", ASCENDING), ("updated_at", DESCENDING)])
        self.evolution_events.create_index([("job_id", ASCENDING), ("sequence", ASCENDING)], unique=True)
        self.evolution_events.create_index([("event_id", ASCENDING)], unique=True)

    def start_run(self, record: dict[str, Any]) -> None:
        self._require(record, "run_id", "_id")
        if record.get("status") != "running":
            raise HistoryError("Run must start in the running state")
        self._insert(self.runs, record, "run")

    def finish_run(self, run_id: str, completion: dict[str, Any]) -> None:
        completion = dict(completion)
        self._require(completion, "status", "completed_at", "trace", "resources")
        lifecycle_entry = completion.pop("lifecycle_entry", None)
        try:
            result = self.runs.update_one(
                {"_id": run_id, "status": "running"},
                {
                    "$set": completion,
                    "$push": {"lifecycle": lifecycle_entry},
                },
            )
        except DatabaseError as exc:
            raise HistoryError("Could not finalize run history") from exc
        if result.matched_count != 1:
            raise HistoryError("Run history record is missing or already finalized")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.find_one({"_id": run_id})

    def bind_run_workflow(self, run_id: str, workflow_revision_id: str) -> None:
        """Bind a run once its immutable workflow has been captured."""

        result = self.runs.update_one(
            {"_id": run_id, "$or": [{"workflow_revision_id": {"$exists": False}}, {"workflow_revision_id": workflow_revision_id}]},
            {"$set": {"workflow_revision_id": workflow_revision_id}},
        )
        if result.matched_count != 1:
            raise HistoryError("Run is already bound to a different workflow")

    def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return compact run history ordered newest-first for the local UI."""

        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        return list(self.runs.find({"status": "completed"}).sort("created_at", DESCENDING).limit(limit))

    def capability_gaps(self, *, task_family: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        query: dict[str, Any] = {"outcome": "unsupported", "limitation_kind": "capability_gap"}
        if task_family:
            query["invocation.task_family"] = task_family
        return list(self.runs.find(query).sort("created_at", DESCENDING).limit(limit))

    def record_eval_case(self, record: dict[str, Any]) -> None:
        record = dict(record)
        self._require(record, "case_id", "_id", "task_family", "dataset", "oracle", "exposure")
        exposure = record.pop("exposure")
        try:
            existing = self.eval_cases.find_one({"_id": record["_id"]})
            if existing:
                immutable = {key: value for key, value in record.items() if key not in {"exposure", "exposures"}}
                if any(existing.get(key) != value for key, value in immutable.items() if key != "created_at"):
                    raise HistoryError("Frozen evaluation case conflicts with existing case")
                self.eval_cases.update_one({"_id": record["_id"]}, {"$push": {"exposures": exposure}})
            else:
                record["exposures"] = [exposure]
                self.eval_cases.insert_one(record)
        except DuplicateKeyError:
            # A concurrent materialization keeps the frozen case and records
            # this additional exposure instead of silently replacing it.
            self.eval_cases.update_one({"_id": record["_id"]}, {"$push": {"exposures": exposure}})
        except DatabaseError as exc:
            raise HistoryError("Could not record evaluation case") from exc

    def record_evaluation(self, record: dict[str, Any]) -> None:
        self._require(record, "evaluation_id", "trial_id", "_id", "run_id", "case_id", "created_at")
        record = dict(record)
        self._insert(self.evaluations, record, "evaluation")
        if self.evaluation_sink is not None:
            evidence = self.evaluation_sink(record)
            self.evaluations.update_one({"_id": record["_id"]}, {"$set": {"phoenix": evidence}})
            if evidence.get("status") == "incomplete":
                raise HistoryError("Phoenix evaluation publication is incomplete")

    def get_eval_case(self, case_id: str) -> dict[str, Any] | None:
        return self.eval_cases.find_one({"_id": case_id})

    def add_case_exposure(self, case_id: str, *, role: str, source: str, at: datetime) -> None:
        result = self.eval_cases.update_one(
            {"_id": case_id}, {"$push": {"exposures": {"role": role, "source": source, "at": at}}}
        )
        if result.matched_count != 1:
            raise HistoryError("Evaluation case is unavailable")

    def record_gap(self, record: dict[str, Any]) -> None:
        self._require(record, "_id", "run_id", "status", "created_at")
        self._insert(self.gaps, record, "capability gap")

    def get_gap(self, run_id: str) -> dict[str, Any] | None:
        return self.gaps.find_one({"_id": run_id})

    def update_gap(self, run_id: str, *, status: str, reason: str, proposal: dict[str, Any] | None = None) -> None:
        patch = {"status": status, "reason": reason, "updated_at": datetime.now().astimezone()}
        if proposal is not None:
            patch["proposal"] = proposal
        result = self.gaps.update_one({"_id": run_id}, {"$set": patch})
        if result.matched_count != 1:
            raise HistoryError("Capability gap is unavailable")

    def record_selection_plan(self, record: dict[str, Any]) -> None:
        self._require(record, "_id", "candidate_id", "candidate_commit", "config_hash", "cases", "created_at")
        self._insert(self.selection_plans, record, "selection plan")

    def record_candidate_result(self, candidate_id: str, result: dict[str, Any]) -> None:
        try:
            changed = self.candidates.update_one(
                {"_id": candidate_id, "selection": {"$exists": False}}, {"$set": {"selection": result}}
            )
        except DatabaseError as exc:
            raise HistoryError("Could not store candidate selection") from exc
        if changed.matched_count != 1:
            raise HistoryError("Candidate selection is missing or already recorded")

    def active_version(self, task_family: str) -> dict[str, Any] | None:
        return self.active_versions.find_one({"_id": task_family})

    def compare_and_swap_active(
        self, *, task_family: str, expected_parent: str, commit: str, evidence_id: str,
        environment_hash: str, at: datetime, workflow_revision_id: str | None = None,
    ) -> bool:
        """Activate only the tested child of the current version."""
        patch = {"commit": commit, "evidence_id": evidence_id, "environment_hash": environment_hash, "updated_at": at}
        if workflow_revision_id is not None:
            patch["workflow_revision_id"] = workflow_revision_id
        try:
            current = self.active_versions.find_one({"_id": task_family})
            if current is None:
                self.active_versions.insert_one({"_id": task_family, **patch, "previous_commit": expected_parent})
                return True
            changed = self.active_versions.update_one(
                {"_id": task_family, "commit": expected_parent},
                {"$set": {**patch, "previous_commit": expected_parent}},
            )
            return changed.modified_count == 1
        except DuplicateKeyError:
            return False
        except DatabaseError as exc:
            raise HistoryError("Could not activate version") from exc

    def record_candidate(self, record: dict[str, Any]) -> None:
        record = dict(record)
        self._require(record, "candidate_id", "_id", "task_family", "changed_mechanism", "created_at")
        record.setdefault("status", "proposed")
        record.setdefault("lifecycle", [{"state": record["status"], "at": record["created_at"]}])
        self._insert(self.candidates, record, "candidate")

    def record_version(self, record: dict[str, Any]) -> None:
        record = dict(record)
        self._require(record, "version_id", "_id", "status", "created_at")
        record.setdefault("lifecycle", [{"state": record["status"], "at": record["created_at"]}])
        self._insert(self.versions, record, "version")

    def record_workflow(self, record: dict[str, Any]) -> dict[str, Any]:
        """Store a deduplicated, immutable graph snapshot."""

        record = dict(record)
        self._require(record, "_id", "workflow_revision_id", "identity_hash", "graph_hash", "graph", "created_at")
        try:
            existing = self.harness_workflows.find_one({"_id": record["_id"]})
            if existing:
                if existing.get("identity_hash") != record["identity_hash"] or existing.get("graph_hash") != record["graph_hash"]:
                    raise HistoryError("Workflow identity conflicts with stored graph")
                return existing
            self.harness_workflows.insert_one(record)
            return record
        except DuplicateKeyError as exc:
            existing = self.harness_workflows.find_one({"identity_hash": record["identity_hash"]})
            if existing and existing.get("graph_hash") == record["graph_hash"]:
                return existing
            raise HistoryError("Workflow identity conflicts with stored graph") from exc
        except DatabaseError as exc:
            raise HistoryError("Could not record harness workflow") from exc

    def get_workflow(self, workflow_revision_id: str) -> dict[str, Any] | None:
        return self.harness_workflows.find_one({"_id": workflow_revision_id})

    def workflows_for(self, task_family: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        return list(self.harness_workflows.find({"task_family": task_family}).sort("created_at", DESCENDING).limit(limit))

    def attach_candidate_workflow(self, candidate_id: str, *, workflow_revision_id: str,
                                  parent_workflow_revision_id: str | None = None,
                                  proposal: dict[str, Any] | None = None) -> None:
        patch: dict[str, Any] = {"workflow_revision_id": workflow_revision_id}
        if parent_workflow_revision_id is not None:
            patch["parent_workflow_revision_id"] = parent_workflow_revision_id
        if proposal is not None:
            patch["workflow_proposal"] = proposal
        result = self.candidates.update_one({"_id": candidate_id}, {"$set": patch})
        if result.matched_count != 1:
            raise HistoryError("Candidate is unavailable for workflow binding")

    def create_evolution_job(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Create at most one durable job for an incident run."""

        record = dict(record)
        self._require(record, "_id", "job_id", "incident_run_id", "status", "created_at", "updated_at")
        record.setdefault("next_event_sequence", 0)
        record.setdefault("lifecycle", [{"state": record["status"], "at": record["created_at"]}])
        try:
            self.evolution_jobs.insert_one(record)
            self.runs.update_one({"_id": record["incident_run_id"]}, {"$set": {"evolution_job_id": record["job_id"]}})
            return record, True
        except DuplicateKeyError:
            existing = self.evolution_jobs.find_one({"incident_run_id": record["incident_run_id"]})
            if existing:
                return existing, False
            raise HistoryError("Evolution job identity conflicts with stored job")
        except DatabaseError as exc:
            raise HistoryError("Could not create evolution job") from exc

    def get_evolution_job(self, job_id: str) -> dict[str, Any] | None:
        return self.evolution_jobs.find_one({"_id": job_id})

    def append_evolution_event(self, job_id: str, *, stage: str, payload: dict[str, Any] | None, at: datetime) -> dict[str, Any]:
        """Append ordered, replayable job progress after its underlying record exists."""

        try:
            updated = self.evolution_jobs.find_one_and_update(
                {"_id": job_id},
                {"$inc": {"next_event_sequence": 1}, "$set": {"updated_at": at}},
                return_document=ReturnDocument.AFTER,
            )
            if updated is None:
                raise HistoryError("Evolution job is unavailable")
            sequence = updated["next_event_sequence"]
            event = {"_id": f"{job_id}:{sequence}", "event_id": f"{job_id}:{sequence}", "job_id": job_id,
                     "sequence": sequence, "stage": stage, "payload": dict(payload or {}), "created_at": at}
            self.evolution_events.insert_one(event)
            return event
        except DuplicateKeyError as exc:
            raise HistoryError("Evolution event identity conflicts") from exc
        except DatabaseError as exc:
            raise HistoryError("Could not append evolution event") from exc

    def evolution_events_after(self, job_id: str, *, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("Evolution event cursor is invalid")
        return list(self.evolution_events.find({"job_id": job_id, "sequence": {"$gt": after}}).sort("sequence", ASCENDING).limit(limit))

    def update_evolution_job(self, job_id: str, *, status: str | None = None,
                             patch: dict[str, Any] | None = None, at: datetime) -> None:
        changes = dict(patch or {})
        changes["updated_at"] = at
        update: dict[str, Any] = {"$set": changes}
        if status is not None:
            changes["status"] = status
            update["$push"] = {"lifecycle": {"state": status, "at": at}}
        result = self.evolution_jobs.update_one({"_id": job_id}, update)
        if result.matched_count != 1:
            raise HistoryError("Evolution job is unavailable")

    def claim_evolution_job(self, job_id: str, *, lease_owner: str, at: datetime) -> bool:
        """Claim a queued job once; a restart can explicitly requeue interrupted work."""

        result = self.evolution_jobs.update_one(
            {"_id": job_id, "status": "queued"},
            {"$set": {"status": "running", "lease_owner": lease_owner, "updated_at": at},
             "$push": {"lifecycle": {"state": "running", "at": at}}},
        )
        return result.modified_count == 1

    def append_candidate_transition(
        self, candidate_id: str, *, status: str, at: datetime, reason: str | None = None
    ) -> None:
        self._append_transition(self.candidates, candidate_id, status=status, at=at, reason=reason)

    def append_version_transition(
        self, version_id: str, *, status: str, at: datetime, reason: str | None = None
    ) -> None:
        self._append_transition(self.versions, version_id, status=status, at=at, reason=reason)

    def candidates_for(
        self, *, task_family: str, changed_mechanism: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        query: dict[str, Any] = {"task_family": task_family}
        if changed_mechanism:
            query["changed_mechanism"] = changed_mechanism
        return list(self.candidates.find(query).sort("created_at", DESCENDING).limit(limit))

    @staticmethod
    def _require(record: dict[str, Any], *keys: str) -> None:
        missing = [key for key in keys if key not in record or record[key] is None]
        if missing:
            raise HistoryError("History record is missing required fields: " + ", ".join(missing))

    @staticmethod
    def _insert(collection: Any, record: dict[str, Any], kind: str) -> None:
        try:
            collection.insert_one(record)
        except DuplicateKeyError as exc:
            raise HistoryError(f"Duplicate immutable {kind} record") from exc
        except DatabaseError as exc:
            raise HistoryError(f"Could not record {kind} history") from exc

    @staticmethod
    def _append_transition(
        collection: Any, record_id: str, *, status: str, at: datetime, reason: str | None
    ) -> None:
        if not status:
            raise ValueError("History transition status is required")
        entry: dict[str, Any] = {"state": status, "at": at}
        if reason:
            entry["reason"] = reason
        try:
            result = collection.update_one({"_id": record_id}, {"$set": {"status": status}, "$push": {"lifecycle": entry}})
        except DatabaseError as exc:
            raise HistoryError("Could not append history transition") from exc
        if result.matched_count != 1:
            raise HistoryError("History record is unavailable for transition")
