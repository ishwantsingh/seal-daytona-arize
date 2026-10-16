"""Durable background execution for browser-triggered evolution jobs."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from self_heal.contracts import utc_now
from self_heal.storage import HistoryStore


ControllerFactory = Callable[[Callable[[str, dict[str, Any]], None]], Any]
CandidateWorkflowCapture = Callable[[dict[str, Any], dict[str, Any]], str | None]
Rerun = Callable[[dict[str, Any], dict[str, Any]], str | None]


class EvolutionJobService:
    """Queues one idempotent job per incident and runs it outside HTTP requests."""

    def __init__(
        self,
        *,
        history: HistoryStore,
        controller_factory: ControllerFactory | None = None,
        capture_candidate_workflow: CandidateWorkflowCapture | None = None,
        rerun: Rerun | None = None,
    ) -> None:
        self.history = history
        self.controller_factory = controller_factory
        self.capture_candidate_workflow = capture_candidate_workflow
        self.rerun = rerun
        self._owner = "web-" + uuid.uuid4().hex
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def queue(self, *, incident_run_id: str, workflow_revision_id: str | None,
              task_family: str | None = None) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        job_id = "evolution_" + uuid.uuid4().hex
        job, created = self.history.create_evolution_job({
            "_id": job_id,
            "job_id": job_id,
            "incident_run_id": incident_run_id,
            "workflow_revision_id": workflow_revision_id,
            "task_family": task_family,
            "status": "queued",
            "stage": "incident_classified",
            "created_at": now,
            "updated_at": now,
        })
        if created:
            self.history.append_evolution_event(job["job_id"], stage="incident_classified", payload={
                "incident_run_id": incident_run_id, "workflow_revision_id": workflow_revision_id,
                "task_family": task_family,
            }, at=now)
            if workflow_revision_id:
                self.history.append_evolution_event(job["job_id"], stage="workflow_snapshot_ready", payload={
                    "workflow_revision_id": workflow_revision_id,
                }, at=utc_now())
            if self.controller_factory is None:
                self.history.update_evolution_job(job["job_id"], status="blocked", patch={
                    "stage": "blocked", "reason": "Evolution worker is not configured",
                }, at=utc_now())
                self.history.append_evolution_event(job["job_id"], stage="blocked", payload={
                    "reason": "Evolution worker is not configured",
                }, at=utc_now())
            else:
                self.start(job["job_id"])
        return self.history.get_evolution_job(job["job_id"]) or job, created

    def resume_pending(self) -> None:
        """Restart jobs that were queued before this server process began."""

        if self.controller_factory is None:
            return
        for job in self.history.evolution_jobs.find({"status": "queued"}):
            self.start(job["job_id"])

    def start(self, job_id: str) -> None:
        with self._lock:
            current = self._threads.get(job_id)
            if current and current.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(job_id,), name=f"self-heal-{job_id[-8:]}", daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _progress(self, job_id: str, stage: str, payload: dict[str, Any]) -> None:
        now = utc_now()
        job = self.history.get_evolution_job(job_id)
        if job is None:
            return
        safe_payload = dict(payload)
        source = safe_payload.pop("candidate_source", None)
        if stage == "candidate_ready" and source and self.capture_candidate_workflow:
            try:
                revision = self.capture_candidate_workflow(job, {**safe_payload, "candidate_source": source})
                if revision:
                    safe_payload["workflow_revision_id"] = revision
                    self.history.attach_candidate_workflow(
                        safe_payload["candidate_id"], workflow_revision_id=revision,
                        parent_workflow_revision_id=job.get("workflow_revision_id"),
                    )
            except Exception as exc:
                safe_payload["workflow_capture_error"] = type(exc).__name__
                self.history.update_evolution_job(job_id, status="blocked", patch={
                    "stage": "blocked", "reason": "Candidate workflow capture failed",
                }, at=now)
                self.history.append_evolution_event(job_id, stage="blocked", payload={
                    "reason": "Candidate workflow capture failed",
                }, at=now)
                raise
        patch = {"stage": stage}
        for key in ("candidate_id", "candidate_commit", "plan_id", "workflow_revision_id"):
            if key in safe_payload:
                patch[key] = safe_payload[key]
        if stage == "proposal_received":
            patch["proposal"] = safe_payload
        self.history.update_evolution_job(job_id, patch=patch, at=now)
        self.history.append_evolution_event(job_id, stage=stage, payload=safe_payload, at=now)

    def _run(self, job_id: str) -> None:
        if not self.history.claim_evolution_job(job_id, lease_owner=self._owner, at=utc_now()):
            return
        job = self.history.get_evolution_job(job_id)
        if job is None:
            return
        self._progress(job_id, "diagnosing", {})
        try:
            assert self.controller_factory is not None
            controller = self.controller_factory(lambda stage, payload: self._progress(job_id, stage, payload))
            result = controller.evolve(job["incident_run_id"])
            status = result.get("status", "open")
            if status == "activated":
                self._progress(job_id, "activated", {
                    "candidate_id": result.get("candidate_id"),
                    "candidate_commit": result.get("candidate_commit"),
                    "plan_id": (result.get("selection") or {}).get("plan_id"),
                    "workflow_revision_id": (result.get("promotion") or {}).get("workflow_revision_id"),
                })
                rerun_id = self.rerun(job, result) if self.rerun else None
                patch = {"result": _safe_result(result), "rerun_run_id": rerun_id}
                self.history.update_evolution_job(job_id, status="activated", patch=patch, at=utc_now())
                self.history.append_evolution_event(job_id, stage="rerun_completed", payload={"rerun_run_id": rerun_id}, at=utc_now())
            else:
                terminal = "needs_contract" if status == "needs_contract" else "rejected" if status == "open" else "blocked"
                self.history.update_evolution_job(job_id, status=terminal, patch={
                    "stage": terminal, "reason": result.get("reason"), "result": _safe_result(result),
                    "candidate_id": result.get("candidate_id"), "candidate_commit": result.get("candidate_commit"),
                    "plan_id": (result.get("selection") or {}).get("plan_id"),
                }, at=utc_now())
                self.history.append_evolution_event(job_id, stage=terminal, payload={
                    "reason": result.get("reason"), "candidate_id": result.get("candidate_id"),
                }, at=utc_now())
        except Exception as exc:
            self.history.update_evolution_job(job_id, status="operational_error", patch={
                "stage": "operational_error", "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            }, at=utc_now())
            self.history.append_evolution_event(job_id, stage="operational_error", payload={
                "reason": type(exc).__name__,
            }, at=utc_now())


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep job snapshots compact; individual trials remain in evaluation history."""

    value = dict(result)
    selection = value.get("selection")
    if isinstance(selection, dict):
        selection = dict(selection)
        selection["trial_count"] = len(selection.pop("trials", []))
        value["selection"] = selection
    return value
