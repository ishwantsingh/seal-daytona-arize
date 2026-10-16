"""Exact-evidence activation and conditional rollback."""

from __future__ import annotations

from typing import Any

from self_heal.contracts import canonical_hash, new_version_id, utc_now
from self_heal.evaluation import SelectionDecision
from self_heal.repository import CandidateRepository, CandidateSource
from self_heal.storage import HistoryStore


class PromotionRejected(ValueError):
    pass


class PromotionManager:
    def __init__(self, history: HistoryStore, repository: CandidateRepository):
        self.history, self.repository = history, repository

    def activate(self, *, task_family: str, candidate_id: str,
                 source: CandidateSource, decision: SelectionDecision,
                 current_environment_hash: str) -> dict[str, Any]:
        self.repository.inspect(source)
        candidate = self.history.candidates.find_one({"_id": candidate_id})
        plan = self.history.selection_plans.find_one({"_id": decision.plan_id})
        if not decision.accepted or decision.reasons:
            raise PromotionRejected("Candidate did not pass selection")
        if not candidate or not plan:
            raise PromotionRejected("Candidate or frozen selection plan is unavailable")
        if (candidate.get("candidate_commit") != source.candidate_commit
            or candidate.get("parent_commit") != source.parent_commit
            or plan["candidate_id"] != candidate_id
            or plan["candidate_commit"] != source.candidate_commit
            or plan["parent_commit"] != source.parent_commit
            or plan["environment_hash"] != decision.environment_hash
            or current_environment_hash != decision.environment_hash):
            raise PromotionRejected("Tested commit or environment identity changed")
        recorded = candidate.get("selection")
        if not recorded or recorded.get("plan_id") != decision.plan_id or not recorded.get("accepted"):
            raise PromotionRejected("Passing trial evidence is not recorded")
        expected = source.parent_commit
        active = self.history.active_version(task_family)
        previous_version = self.history.versions.find_one({
            "task_family": task_family, "commit": expected, "status": "active"
        })
        if active and active["commit"] != expected:
            raise PromotionRejected("Active parent changed; candidate requires reevaluation")
        if not active and not self.history.versions.find_one({"commit": expected, "status": "baseline"}):
            baseline_id = new_version_id()
            baseline = {
                "_id": baseline_id, "version_id": baseline_id, "commit": expected,
                "task_family": task_family, "identity_hash": canonical_hash({"baseline": expected}),
                "status": "baseline", "created_at": utc_now(),
            }
            if candidate.get("parent_workflow_revision_id"):
                baseline["workflow_revision_id"] = candidate["parent_workflow_revision_id"]
            self.history.record_version(baseline)
        workflow_revision_id = candidate.get("workflow_revision_id")
        version_id = new_version_id()
        version = {
            "_id": version_id, "version_id": version_id, "commit": source.candidate_commit,
            "parent_commit": expected, "candidate_id": candidate_id,
            "task_family": task_family, "identity_hash": decision.environment_hash,
            "environment_hash": decision.environment_hash, "selection_plan_id": decision.plan_id,
            "status": "tested", "created_at": utc_now(),
        }
        if workflow_revision_id:
            version["workflow_revision_id"] = workflow_revision_id
            version["parent_workflow_revision_id"] = candidate.get("parent_workflow_revision_id")
        self.history.record_version(version)
        if not self.history.compare_and_swap_active(
            task_family=task_family, expected_parent=expected, commit=source.candidate_commit,
            evidence_id=decision.plan_id, environment_hash=decision.environment_hash, at=utc_now(),
            workflow_revision_id=workflow_revision_id,
        ):
            self.history.append_version_transition(version_id, status="stale", at=utc_now(),
                                                   reason="Active parent changed")
            raise PromotionRejected("Active parent changed; candidate requires reevaluation")
        self.history.append_version_transition(version_id, status="active", at=utc_now())
        if previous_version:
            self.history.append_version_transition(previous_version["version_id"], status="superseded", at=utc_now())
        self.history.append_candidate_transition(candidate_id, status="accepted", at=utc_now())
        return {"version_id": version_id, "active_commit": source.candidate_commit,
                "previous_commit": expected, "selection_plan_id": decision.plan_id,
                "workflow_revision_id": workflow_revision_id}

    def rollback(self, *, task_family: str, expected_active: str, target_commit: str,
                 reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise PromotionRejected("Rollback reason is required")
        target = self.history.versions.find_one({"task_family": task_family, "commit": target_commit})
        if not target or target.get("status") not in {"baseline", "active", "superseded"}:
            raise PromotionRejected("Rollback target is not a retained version")
        self.repository.resolve_commit(target_commit)
        previous_version = self.history.versions.find_one({
            "task_family": task_family, "commit": expected_active, "status": "active"
        })
        if not self.history.compare_and_swap_active(
            task_family=task_family, expected_parent=expected_active, commit=target_commit,
            evidence_id="rollback:" + target["version_id"],
            environment_hash=target.get("environment_hash", "baseline"), at=utc_now(),
            workflow_revision_id=target.get("workflow_revision_id"),
        ):
            raise PromotionRejected("Active version changed before rollback")
        rollback_id = new_version_id()
        self.history.record_version({
            "_id": rollback_id, "version_id": rollback_id, "commit": target_commit,
            "parent_commit": expected_active, "task_family": task_family,
            "workflow_revision_id": target.get("workflow_revision_id"),
            "status": "rollback", "reason": reason, "created_at": utc_now(),
        })
        if previous_version:
            self.history.append_version_transition(previous_version["version_id"], status="superseded", at=utc_now(),
                                                   reason=reason)
        if target["status"] == "superseded":
            self.history.append_version_transition(target["version_id"], status="active", at=utc_now(), reason="rollback")
        return {"version_id": rollback_id, "active_commit": target_commit,
                "previous_commit": expected_active, "reason": reason,
                "workflow_revision_id": target.get("workflow_revision_id")}
