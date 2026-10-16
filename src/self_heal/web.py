"""Local web surface for the trusted Self-Heal supervisor.

The browser is deliberately thin: every run still travels through the same
execution path used by the CLI, and the only history exposed is the compact
evidence record intended for operator inspection. It is a local
operator interface, not an internet-facing authentication boundary.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sysconfig
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from self_heal.database import ASCENDING

from harness.agent import RunResult
from harness.tools import AnalystTools
from harness.logistics import LogisticsAgent, LogisticsTools, TOOL_VERSION
from self_heal.execution import RunExecution, RunExecutor, baseline_source_commit
from self_heal.model import ChatModel
from self_heal.settings import AnalystConfig, PhoenixConfig
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore, DatasetError, DatasetInfo
from self_heal.telemetry import PhoenixTelemetry
from self_heal.repository import CandidateRepository
from self_heal.runner import CandidateRunner
from self_heal.evidence import safe_payload
from self_heal.logistics_store import DatasetBundleInfo, LogisticsDatasetStore
from self_heal.contracts import canonical_hash
from self_heal.evolution_jobs import EvolutionJobService
from self_heal.workflow import extract_workflow, proposal_overlay, workflow_diff
from evals.logistics.generator import public_incident_bundle


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ASSET_ROOT = PROJECT_ROOT / "ui"
_INSTALLED_ASSET_ROOT = Path(sysconfig.get_path("data")) / "share" / "self-heal" / "ui"
ASSET_ROOT = _SOURCE_ASSET_ROOT if _SOURCE_ASSET_ROOT.is_dir() else _INSTALLED_ASSET_ROOT
MAX_REQUEST_BYTES = 8_192
PROTECTED_DATASET_PREFIXES = ("eval-", "incident-", "private-", "final-")


def _operator_dataset(dataset_id: str) -> bool:
    return not dataset_id.casefold().startswith(PROTECTED_DATASET_PREFIXES)


class WebRequestError(ValueError):
    """An input error that is safe to present to a local operator."""


def _result_message(result: RunResult) -> str:
    if result.outcome == "unsupported":
        if result.limitation_kind == "clarification_required":
            return result.limitation_reason or "Please clarify the question."
        return "I can't answer that with my current capabilities."
    if result.outcome == "error":
        return f"Sorry, I couldn't complete that request: {result.error}."
    assert result.answer is not None and result.interpreted_task is not None
    task, answer = result.interpreted_task, result.answer
    if task.get("operation") == "count_customers_with_shipment_count_gt":
        noun = "customer" if answer["value"] == 1 else "customers"
        return (f"{answer['value']} {noun} sent more than {task['threshold']} shipments "
                f"from warehouse {task['warehouse_number']} yesterday.")
    if "metric" not in task:
        if "value" in answer:
            return f"The result is {answer['value']}."
        return "; ".join(f"{key}: {value}" for key, value in answer["groups"].items()) or "No matching groups."
    metric = {"available": "available", "on_hand": "on-hand", "reserved": "reserved"}[task["metric"]]
    location = {
        "warehouse": f" in the {task.get('filter_value')} warehouse",
        "category": f" in the {task.get('filter_value')} category",
        "sku": f" for SKU {task.get('filter_value')}",
    }.get(task.get("filter_field"), "")
    if "groups" in answer:
        groups = answer["groups"]
        if not groups:
            return "I found no matching rows to group."
        values = "; ".join(f"{name}: {value}" for name, value in groups.items())
        return f"{metric.capitalize()} units by {task['group_by'].replace('_', ' ')}{location}: {values}."
    value = answer["value"]
    return f"There are {value} {metric} {'unit' if value == 1 else 'units'}{location or ' in this dataset'}."


def _run_payload(execution: RunExecution) -> dict[str, Any]:
    result = execution.result
    return {
        "run_id": result.run_id,
        "outcome": result.outcome,
        "message": _result_message(result),
        "answer": result.answer,
        "error": result.error,
        "interpreted_task": result.interpreted_task,
        "limitation_kind": result.limitation_kind,
        "limitation_reason": result.limitation_reason,
        "capability_request": result.capability_request,
        "resources": {
            "model_calls": result.model_calls,
            "tool_calls": result.tool_calls,
            "total_tokens": result.total_tokens,
            "elapsed_seconds": result.elapsed_seconds,
            "table_pages": result.table_pages,
            "table_bytes": result.table_bytes,
        },
        **execution.compact_evidence(),
    }


def _history_summary(record: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    invocation = record.get("invocation") or {}
    trace = record.get("trace") or {}
    answer = record.get("answer") or {}
    if record.get("outcome") == "answered":
        task = record.get("interpreted_task") or {}
        if "value" in answer and task.get("operation") == "count_customers_with_shipment_count_gt":
            noun = "customer" if answer["value"] == 1 else "customers"
            message = (f"{answer['value']} {noun} sent more than {task['threshold']} shipments "
                       f"from warehouse {task['warehouse_number']} yesterday.")
        elif "value" in answer and task.get("metric") in {"available", "on_hand", "reserved"}:
            metric = {"available": "available", "on_hand": "on-hand", "reserved": "reserved"}[task["metric"]]
            location = (f" in the {task['filter_value']} warehouse" if task.get("filter_field") == "warehouse"
                        else f" in the {task['filter_value']} category" if task.get("filter_field") == "category"
                        else f" for SKU {task['filter_value']}" if task.get("filter_field") == "sku" else "")
            message = f"{answer['value']} {metric} units{location or ' in this dataset'}."
        elif "value" in answer:
            message = f"Answer: {answer['value']}"
        elif "groups" in answer:
            message = "; ".join(f"{key}: {value}" for key, value in answer["groups"].items()) or "No matching groups."
        else:
            message = "No answer was stored."
    elif record.get("outcome") == "unsupported":
        message = record.get("limitation_reason") or "The analyst cannot answer with its current capabilities."
    else:
        message = record.get("error") or "No answer was stored."
    return {
        "run_id": record.get("run_id"),
        "created_at": record.get("created_at"),
        "outcome": record.get("outcome"),
        "answer": record.get("answer"),
        "message": message,
        "error": record.get("error"),
        "question": invocation.get("question"),
        "task": record.get("interpreted_task"),
        "dataset": record.get("dataset"),
        "resources": record.get("resources"),
        "limitation_kind": record.get("limitation_kind"),
        "limitation_reason": record.get("limitation_reason"),
        "capability_request": record.get("capability_request"),
        "trace": {
            "id": trace.get("root_id"),
            "url": trace.get("url"),
            "status": trace.get("status"),
            "project": trace.get("project"),
            "error_type": trace.get("error_type"),
        },
        "history_status": record.get("status"),
        "workflow_revision_id": record.get("workflow_revision_id"),
        "evolution_job_id": record.get("evolution_job_id"),
        "version": ((record.get("execution") or {}).get("source", {}).get("commit") or
                    (TOOL_VERSION if (record.get("dataset") or {}).get("input_kind") == "logistics_bundle"
                     else (record.get("execution") or {}).get("config", {}).get("task_contract_version"))),
        **({"evidence": record.get("evidence")} if detail else {}),
    }


def _workflow_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_revision_id": record.get("workflow_revision_id"),
        "task_family": record.get("task_family"),
        "source_commit": record.get("source_commit"),
        "configuration_hash": record.get("configuration_hash"),
        "graph_hash": record.get("graph_hash"),
        "created_at": record.get("created_at"),
        "node_count": len((record.get("graph") or {}).get("nodes") or []),
        "edge_count": len((record.get("graph") or {}).get("edges") or []),
    }


def _event_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {"event_id": record.get("event_id"), "sequence": record.get("sequence"),
            "stage": record.get("stage"), "payload": record.get("payload") or {},
            "created_at": record.get("created_at")}


def _safe_job(record: dict[str, Any]) -> dict[str, Any]:
    fields = ("job_id", "incident_run_id", "task_family", "workflow_revision_id", "status", "stage", "reason",
              "candidate_id", "candidate_commit", "plan_id", "rerun_run_id", "proposal", "result",
              "created_at", "updated_at", "next_event_sequence")
    return {field: record.get(field) for field in fields if record.get(field) is not None}


class WebApplication:
    """A testable adapter from the local UI's API to trusted supervisor APIs."""

    def __init__(
        self,
        *,
        store: TableStore,
        history: HistoryStore,
        config: AnalystConfig,
        telemetry: PhoenixTelemetry,
        model_factory: Callable[[], ChatModel],
        tracing: PhoenixConfig,
        logistics: LogisticsDatasetStore | None = None,
        evolution_controller_factory: Callable[[Callable[[str, dict[str, Any]], None]], Any] | None = None,
    ) -> None:
        self.store = store
        self.history = history
        self.config = config
        self.telemetry = telemetry
        self.model_factory = model_factory
        self.tracing = tracing
        self.logistics = logistics
        self.evolution_jobs = EvolutionJobService(
            history=history,
            controller_factory=evolution_controller_factory,
            capture_candidate_workflow=self._capture_candidate_workflow,
            rerun=self._rerun_evolved_incident,
        )
        self.evolution_jobs.resume_pending()

    def _config_for_family(self, task_family: str) -> AnalystConfig:
        if task_family != "logistics-shipment-threshold":
            return self.config
        contract = (self.config.task_contracts or {}).get("logistics-shipment-threshold-v1")
        if not contract:
            raise WebRequestError("Logistics task contract is unavailable")
        return replace(self.config, task_family="logistics-shipment-threshold",
                       task_contract_version="logistics-shipment-threshold-v1",
                       contract_hash=canonical_hash(contract))

    def _ensure_workflow(self, *, task_family: str, source: Path, source_commit: str) -> str:
        snapshot = extract_workflow(source=source, source_commit=source_commit,
                                    config=self._config_for_family(task_family), task_family=task_family)
        stored = self.history.record_workflow(snapshot.record)
        return stored["workflow_revision_id"]

    def _capture_candidate_workflow(self, job: dict[str, Any], payload: dict[str, Any]) -> str | None:
        candidate_id, commit, source = payload.get("candidate_id"), payload.get("candidate_commit"), payload.get("candidate_source")
        if not isinstance(candidate_id, str) or not isinstance(commit, str) or not isinstance(source, str):
            raise WebRequestError("Candidate workflow metadata is incomplete")
        candidate_source = Path(source).resolve()
        if not (candidate_source / "harness").is_dir():
            raise WebRequestError("Candidate harness source is unavailable")
        family = (self.history.get_evolution_job(job["job_id"]) or job).get("task_family") or self.config.task_family
        revision = self._ensure_workflow(task_family=family, source=candidate_source, source_commit=commit)
        candidate = self.history.candidates.find_one({"_id": candidate_id}) or {}
        self.history.attach_candidate_workflow(
            candidate_id, workflow_revision_id=revision,
            parent_workflow_revision_id=job.get("workflow_revision_id"),
            proposal=proposal_overlay(
                base_revision_id=job.get("workflow_revision_id") or "unknown",
                changed_mechanism=candidate.get("changed_mechanism", "harness change"),
                hypothesis=candidate.get("hypothesis", ""), diff=candidate.get("diff"),
            ),
        )
        return revision

    def _rerun_evolved_incident(self, job: dict[str, Any], result: dict[str, Any]) -> str | None:
        """Run one pinned post-activation verification without creating another job."""

        observed = self.history.get_run(job["incident_run_id"])
        if not observed:
            return None
        invocation = observed.get("invocation") or {}
        question = invocation.get("question")
        dataset = observed.get("dataset") or {}
        if not isinstance(question, str) or not isinstance(dataset.get("id"), str):
            return None
        request = {"question": question, "dataset_id": dataset["id"]}
        if dataset.get("input_kind") == "logistics_bundle":
            request["input_kind"] = "logistics_bundle"
        try:
            promotion = result.get("promotion") or {}
            commit = promotion.get("active_commit")
            workflow_revision_id = promotion.get("workflow_revision_id")
            if commit:
                source = CandidateRepository(PROJECT_ROOT).active_checkout(commit)
                logistics_input = request.get("input_kind") == "logistics_bundle"
                if logistics_input:
                    if self.logistics is None:
                        return None
                    pinned_dataset = self.logistics.dataset_info(dataset["id"])
                    config = self._config_for_family("logistics-shipment-threshold")
                else:
                    pinned_dataset = self.store.dataset_info(dataset["id"])
                    config = self.config
                execution = CandidateRunner(
                    store=self.store, logistics=self.logistics, config=config, history=self.history, telemetry=self.telemetry,
                    image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
                ).run(source=source, source_commit=commit, dataset=pinned_dataset,
                      invocation=question, model=self.model_factory(),
                      workflow_revision_id=workflow_revision_id)
            else:
                execution, _ = self._run(request)
            return execution.result.run_id
        except Exception:
            return None

    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """Dispatch a small same-origin API.  This method has no HTTP concerns."""

        route = urlsplit(path)
        query = parse_qs(route.query)
        if method == "GET" and route.path == "/api/health":
            active = self.history.active_version(self.config.task_family)
            logistics_active = self.history.active_version("logistics-shipment-threshold")
            return HTTPStatus.OK, {
                "sqlite": "connected",
                "phoenix": "enabled" if self.tracing.enabled else "disabled",
                "task_contract_version": self.config.task_contract_version,
                "active_version": active.get("commit") if active else self.config.task_contract_version,
                "logistics_active_version": logistics_active.get("commit") if logistics_active else TOOL_VERSION,
            }
        if method == "GET" and route.path == "/api/datasets":
            return HTTPStatus.OK, {
                "datasets": ([
                    {"id": item.dataset_id, "row_count": item.row_count, "content_hash": item.content_hash,
                     "input_kind": "inventory_table", "domain": "inventory"}
                    for item in self.store.list_dataset_info() if _operator_dataset(item.dataset_id)
                ] + ([{"id": item.dataset_id, "row_count": item.row_count,
                       "content_hash": item.content_hash, "input_kind": "logistics_bundle",
                       "domain": item.domain, "relations": {name: value["row_count"] for name, value in item.relations.items()}}
                      for item in self.logistics.list_dataset_info() if _operator_dataset(item.dataset_id)]
                     if self.logistics is not None else []))
            }
        if method == "GET" and route.path == "/api/datasets/preview":
            dataset_id = query.get("dataset_id", [""])[0]
            input_kind = query.get("input_kind", [""])[0]
            relation = query.get("relation", [""])[0]
            if not dataset_id or len(dataset_id) > 100 or not _operator_dataset(dataset_id):
                raise WebRequestError("Dataset is unavailable for preview")
            if input_kind == "inventory_table":
                if relation not in {"", "analyst_rows"}:
                    raise WebRequestError("Inventory relation is invalid")
                info = self.store.dataset_info(dataset_id)
                rows = self.store.verified_rows(dataset_id)
                return HTTPStatus.OK, {
                    "dataset_id": dataset_id, "input_kind": input_kind,
                    "relation": "analyst_rows", "relations": {"analyst_rows": info.row_count},
                    "columns": list(self.config.table_schema), "row_count": info.row_count,
                    "rows": rows[:20],
                }
            if input_kind == "logistics_bundle" and self.logistics is not None:
                selected = relation or "shipments"
                if selected not in {"customers", "warehouses", "shipments"}:
                    raise WebRequestError("Logistics relation is invalid")
                info = self.logistics.dataset_info(dataset_id)
                rows = self.logistics.verified_relations(dataset_id)[selected]
                return HTTPStatus.OK, {
                    "dataset_id": dataset_id, "input_kind": input_kind,
                    "relation": selected,
                    "relations": {name: value["row_count"] for name, value in info.relations.items()},
                    "columns": list(rows[0]) if rows else [],
                    "row_count": info.relations[selected]["row_count"], "rows": rows[:20],
                }
            raise WebRequestError("Dataset type is invalid")
        if method == "GET" and route.path == "/api/harness-workflows":
            family = query.get("task_family", [self.config.task_family])[0]
            if family not in {self.config.task_family, "logistics-shipment-threshold"}:
                raise WebRequestError("Task family is invalid")
            active = self.history.active_version(family) or {}
            workflows = self.history.workflows_for(family, limit=_limit(query))
            return HTTPStatus.OK, {
                "task_family": family,
                "active_workflow_revision_id": active.get("workflow_revision_id"),
                "active_commit": active.get("commit"),
                "workflows": [_workflow_summary(item) for item in workflows],
            }
        if method == "GET" and route.path.startswith("/api/harness-workflows/") and route.path.endswith("/diff"):
            revision_id = route.path.removeprefix("/api/harness-workflows/").removesuffix("/diff").rstrip("/")
            base_id = query.get("base", [None])[0]
            if not revision_id or not base_id or "/" in revision_id or "/" in base_id:
                raise WebRequestError("Workflow comparison is invalid")
            candidate, base = self.history.get_workflow(revision_id), self.history.get_workflow(base_id)
            if not candidate or not base:
                return HTTPStatus.NOT_FOUND, {"error": "Workflow revision is unavailable"}
            if candidate.get("task_family") != base.get("task_family"):
                raise WebRequestError("Workflows belong to different task families")
            return HTTPStatus.OK, workflow_diff(base, candidate)
        if method == "GET" and route.path.startswith("/api/harness-workflows/"):
            revision_id = route.path.removeprefix("/api/harness-workflows/")
            if not revision_id or "/" in revision_id:
                raise WebRequestError("Workflow revision is invalid")
            record = self.history.get_workflow(revision_id)
            if not record:
                return HTTPStatus.NOT_FOUND, {"error": "Workflow revision is unavailable"}
            return HTTPStatus.OK, {**_workflow_summary(record), "graph": record.get("graph")}
        if method == "GET" and route.path.startswith("/api/evolution-jobs/") and route.path.endswith("/events"):
            job_id = route.path.removeprefix("/api/evolution-jobs/").removesuffix("/events").rstrip("/")
            if not job_id or "/" in job_id:
                raise WebRequestError("Evolution job ID is invalid")
            job = self.history.get_evolution_job(job_id)
            if not job:
                return HTTPStatus.NOT_FOUND, {"error": "Evolution job is unavailable"}
            after = _cursor(query, "after")
            events = self.history.evolution_events_after(job_id, after=after, limit=_limit(query))
            return HTTPStatus.OK, {"job_id": job_id, "events": [_event_summary(item) for item in events],
                                   "next_cursor": events[-1]["sequence"] if events else after}
        if method == "GET" and route.path.startswith("/api/evolution-jobs/") and route.path.endswith("/evaluations"):
            job_id = route.path.removeprefix("/api/evolution-jobs/").removesuffix("/evaluations").rstrip("/")
            if not job_id or "/" in job_id:
                raise WebRequestError("Evolution job ID is invalid")
            job = self.history.get_evolution_job(job_id)
            if not job:
                return HTTPStatus.NOT_FOUND, {"error": "Evolution job is unavailable"}
            return HTTPStatus.OK, self._job_evaluations(job, limit=_limit(query))
        if method == "GET" and route.path.startswith("/api/evolution-jobs/"):
            job_id = route.path.removeprefix("/api/evolution-jobs/")
            if not job_id or "/" in job_id:
                raise WebRequestError("Evolution job ID is invalid")
            job = self.history.get_evolution_job(job_id)
            if not job:
                return HTTPStatus.NOT_FOUND, {"error": "Evolution job is unavailable"}
            payload = _safe_job(job)
            for key in ("workflow_revision_id", "candidate_id", "plan_id", "rerun_run_id"):
                if job.get(key):
                    payload[key + "_url"] = ("/api/harness-workflows/" + job[key] if key == "workflow_revision_id"
                                              else "/api/runs/" + job[key] if key == "rerun_run_id" else None)
            candidate = self.history.candidates.find_one({"_id": job.get("candidate_id")}) if job.get("candidate_id") else None
            if candidate:
                payload["candidate"] = {"candidate_id": candidate.get("candidate_id"),
                                        "candidate_commit": candidate.get("candidate_commit"),
                                        "parent_commit": candidate.get("parent_commit"),
                                        "workflow_revision_id": candidate.get("workflow_revision_id"),
                                        "parent_workflow_revision_id": candidate.get("parent_workflow_revision_id"),
                                        "workflow_proposal": candidate.get("workflow_proposal"),
                                        "hypothesis": candidate.get("hypothesis"),
                                        "changed_mechanism": candidate.get("changed_mechanism"),
                                        "status": candidate.get("status")}
            return HTTPStatus.OK, payload
        if method == "GET" and route.path == "/api/runs":
            return HTTPStatus.OK, {"runs": [_history_summary(item) for item in self.history.recent_runs(limit=_limit(query))]}
        if method == "GET" and route.path == "/api/evaluations":
            records = self.history.evaluations.find({}).sort("created_at", -1).limit(_limit(query))
            ordinary = [{
                "evaluation_id": item.get("evaluation_id"), "case_id": item.get("case_id"),
                "run_id": item.get("run_id"), "passed": item.get("passed"),
                "violation": item.get("violation"), "resources": item.get("resources"),
                "trace_id": item.get("trace_id"), "created_at": item.get("created_at"),
                "role": item.get("role") or (item.get("candidate") or {}).get("role") or "Selection / baseline",
            } for item in records]
            finals = self.history.final_assessments.find({"status": {"$in": ["completed", "failed"]}}).sort("started_at", -1).limit(_limit(query))
            final_rows = [{"evaluation_id": item.get("case_id"), "case_id": item.get("case_id"),
                           "run_id": item.get("run_id"), "passed": item.get("passed"),
                           "violation": item.get("violation"), "resources": item.get("resources"),
                           "trace_id": item.get("trace_id"), "created_at": item.get("completed_at"),
                           "role": "Final assessment"} for item in finals]
            return HTTPStatus.OK, {"evaluations": sorted(ordinary + final_rows,
                              key=lambda item: str(item.get("created_at") or ""),
                              reverse=True)[:_limit(query)]}
        if method == "GET" and route.path == "/api/versions":
            family = query.get("task_family", [self.config.task_family])[0]
            if family not in {self.config.task_family, "logistics-shipment-threshold"}:
                raise WebRequestError("Task family is invalid")
            active = self.history.active_version(family)
            records = self.history.versions.find({"task_family": family}).sort("created_at", -1).limit(_limit(query))
            return HTTPStatus.OK, {"active_commit": active.get("commit") if active else None,
                                   "base_version": TOOL_VERSION if family == "logistics-shipment-threshold" else self.config.task_contract_version,
                                   "versions": [{"version_id": item.get("version_id"),
                                                 "commit": item.get("commit"), "status": item.get("status"),
                                                 "parent_commit": item.get("parent_commit"),
                                                 "workflow_revision_id": item.get("workflow_revision_id"),
                                                 "created_at": item.get("created_at")} for item in records]}
        if method == "GET" and route.path == "/api/capability-gaps":
            return HTTPStatus.OK, {
                "runs": [_history_summary(item) for item in self.history.capability_gaps(limit=_limit(query))]
            }
        if method == "GET" and route.path.startswith("/api/runs/"):
            run_id = route.path.removeprefix("/api/runs/")
            if not run_id or "/" in run_id:
                raise WebRequestError("Run ID is invalid")
            record = self.history.get_run(run_id)
            if record is None:
                return HTTPStatus.NOT_FOUND, {"error": "Run history is unavailable"}
            payload = _history_summary(record, detail=True)
            if record.get("outcome") == "unsupported":
                payload["gap"] = self._gap_evidence(run_id, (record.get("invocation") or {}).get("task_family"))
            trace = record.get("trace") or {}
            if trace.get("status") == "available" and trace.get("root_id"):
                try:
                    payload["spans"] = self.telemetry.trace_spans(trace["root_id"])
                except Exception:
                    payload["spans"] = None
            return HTTPStatus.OK, payload
        if method == "GET" and route.path.startswith("/api/evaluation-cases/"):
            case_id = route.path.removeprefix("/api/evaluation-cases/")
            record = self.history.get_eval_case(case_id) if case_id and "/" not in case_id else None
            if not record:
                return HTTPStatus.NOT_FOUND, {"error": "Evaluation case is unavailable"}
            return HTTPStatus.OK, {"case_id": case_id, "scenario_id": record.get("scenario_id"),
                                   "origin": record.get("origin"), "task": safe_payload(record.get("task")),
                                   "dataset": record.get("dataset")}
        if method == "GET" and route.path.startswith("/api/candidates/") and route.path.endswith("/diff"):
            candidate_id = route.path.removeprefix("/api/candidates/").removesuffix("/diff").rstrip("/")
            record = self.history.candidates.find_one({"_id": candidate_id}) if candidate_id and "/" not in candidate_id else None
            if not record or not record.get("diff"):
                return HTTPStatus.NOT_FOUND, {"error": "Candidate diff is unavailable"}
            return HTTPStatus.OK, {"candidate_id": candidate_id, "diff": safe_payload(record["diff"])}
        if method == "POST" and route.path == "/api/runs":
            request = _run_request(body)
            execution, dataset = self._run(request)
            payload = _run_payload(execution)
            payload["question"] = request["question"]
            payload["dataset"] = {
                "id": dataset.dataset_id,
                "row_count": dataset.row_count,
                "input_kind": getattr(dataset, "input_kind", "inventory_table"),
                "domain": getattr(dataset, "domain", "inventory"),
                "relations": {name: value["row_count"] for name, value in dataset.relations.items()} if hasattr(dataset, "relations") else None,
            }
            record = self.history.get_run(payload["run_id"])
            if record:
                payload = _history_summary(record, detail=True) | {"history": payload["history"], "message": payload["message"],
                                                        "dataset": payload["dataset"],
                                                        "gap": self._gap_evidence(payload["run_id"], (record.get("invocation") or {}).get("task_family")) if payload["outcome"] == "unsupported" else None}
                if self._eligible_for_evolution(record):
                    job, _ = self.evolution_jobs.queue(
                        incident_run_id=record["run_id"], workflow_revision_id=record.get("workflow_revision_id"),
                        task_family=(record.get("invocation") or {}).get("task_family"),
                    )
                    payload["evolution_job_id"] = job["job_id"]
                    payload["evolution_url"] = "#evolve/" + job["job_id"]
                    payload["evolution_state"] = job.get("status")
                    payload["evolution_reason"] = job.get("reason")
            return HTTPStatus.CREATED, payload
        if method == "POST" and route.path == "/api/datasets/logistics":
            if body != {} or self.logistics is None:
                raise WebRequestError("Logistics demo dataset is unavailable")
            info = self.logistics.materialize("logistics-shipment-threshold-public-v1", **public_incident_bundle())
            return HTTPStatus.CREATED, {"id": info.dataset_id, "input_kind": info.input_kind,
                                        "relations": {name: value["row_count"] for name, value in info.relations.items()}}
        return HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"}

    def _gap_evidence(self, run_id: str, task_family: str | None = None) -> dict[str, Any]:
        gap = self.history.get_gap(run_id)
        candidates = list(self.history.candidates.find({"incident_run_id": run_id}).sort("created_at", -1).limit(10))
        active = self.history.active_version(task_family or self.config.task_family)
        cases = list(self.history.eval_cases.find({"scenario_id": {"$regex": "^observed-" + re.escape(run_id.replace("-", "")[:24])}}))
        result = []
        for candidate in candidates:
            selection = candidate.get("selection") or {}
            trials = selection.get("trials") or []
            def status(roles: set[str]) -> str:
                matching = [trial for trial in trials if trial.get("version") == "candidate" and trial.get("role") in roles]
                return "not tested" if not matching else ("passed" if all(trial.get("passed") for trial in matching) else "failed")
            result.append({
                "id": candidate["candidate_id"], "status": "Rejected" if candidate.get("status") == "rejected" or selection.get("accepted") is False else candidate.get("status", "proposed"),
                "correctness": status({"original", "generated_reproduction"}),
                "regression": status({"regression", "private_validation", "negative"}),
                "reasons": selection.get("reasons") or ([candidate["rejection_reason"]] if candidate.get("rejection_reason") else []),
                "diff_url": "/api/candidates/" + candidate["candidate_id"] + "/diff" if candidate.get("diff") else None,
            })
        return {"status": gap.get("status") if gap else "not evaluated",
                "active_version": active.get("commit") if active else ("logistics-shipment-threshold-v1" if task_family == "logistics-shipment-threshold" else self.config.task_contract_version),
                "cases": [{"id": case["case_id"], "url": "/api/evaluation-cases/" + case["case_id"]} for case in cases],
                "candidates": result}

    @staticmethod
    def _eligible_for_evolution(record: dict[str, Any]) -> bool:
        if record.get("outcome") == "unsupported":
            return record.get("limitation_kind") == "capability_gap"
        if record.get("outcome") == "error":
            error = (record.get("error") or "").lower()
            return bool(error) and not any(word in error for word in ("sqlite", "network", "provider", "trace", "model or runtime failure"))
        return False

    def _job_evaluations(self, job: dict[str, Any], *, limit: int) -> dict[str, Any]:
        candidate = self.history.candidates.find_one({"_id": job.get("candidate_id")}) if job.get("candidate_id") else None
        plan_id = job.get("plan_id") or ((candidate or {}).get("selection") or {}).get("plan_id")
        # A plan is persisted before trials start; the terminal selection result
        # is stored only after all trials finish. Resolve it by candidate while
        # evaluation is running so polling can expose real partial progress.
        plan = (self.history.selection_plans.find_one({"_id": plan_id}) if plan_id
                else self.history.selection_plans.find_one({"candidate_id": job.get("candidate_id")})
                if job.get("candidate_id") else None)
        if not plan_id and plan:
            plan_id = plan["_id"]
        if not plan_id:
            return {"plan_id": None, "watermark": 0, "scheduled": {}, "groups": [], "trials": []}
        if not plan:
            return {"plan_id": plan_id, "watermark": 0, "scheduled": {}, "groups": [], "trials": []}
        role_by_case = {case["case_id"]: case.get("role", "selection") for case in plan.get("cases", [])}
        scheduled: dict[str, dict[str, int]] = {"baseline": {}, "candidate": {}}
        for role in role_by_case.values():
            repeats = self.config.evaluation.live_repetitions if role in {"original", "private_validation"} else 1
            for version in scheduled.values():
                version[role] = version.get(role, 0) + repeats
        records = list(self.history.evaluations.find({"plan_id": plan_id}).sort("created_at", ASCENDING))
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        safe_trials = []
        for item in records:
            candidate_record = item.get("candidate") or {}
            version = candidate_record.get("version", "candidate")
            role = item.get("case_role") or role_by_case.get(item.get("case_id"), "selection")
            key = (version, role)
            stats = grouped.setdefault(key, {"version": version, "role": role, "completed": 0, "passed": 0, "failed": 0,
                                             "elapsed_seconds": [], "total_tokens": [], "table_pages": []})
            stats["completed"] += 1
            stats["passed" if item.get("passed") else "failed"] += 1
            resources = item.get("resources") or {}
            for metric in ("elapsed_seconds", "total_tokens", "table_pages"):
                if isinstance(resources.get(metric), (int, float)):
                    stats[metric].append(resources[metric])
            protected = role == "private_validation"
            safe_trials.append({
                "trial_id": item.get("trial_id"), "case_id": None if protected else item.get("case_id"),
                "case_label": "Private validation" if protected else item.get("case_id"),
                "role": role, "version": version, "repeat": item.get("repeat"),
                "passed": item.get("passed"), "status": "completed", "violation": None if protected else item.get("violation"),
                "run_id": item.get("run_id"), "resources": resources, "trace_id": item.get("trace_id"),
                "created_at": item.get("created_at"),
            })
        groups = []
        for (version, role), stats in sorted(grouped.items()):
            expected = scheduled.get(version, {}).get(role, 0)
            groups.append({
                "version": version, "role": role, "expected": expected, "completed": stats["completed"],
                "passed": stats["passed"], "failed": stats["failed"],
                "pending": max(expected - stats["completed"], 0),
                "metrics": {metric: round(sum(values) / len(values), 3) if values else None
                            for metric, values in (("elapsed_seconds", stats["elapsed_seconds"]),
                                                   ("total_tokens", stats["total_tokens"]),
                                                   ("table_pages", stats["table_pages"]))},
            })
        return {"plan_id": plan_id, "watermark": len(records), "scheduled": scheduled,
                "groups": groups, "trials": safe_trials[-limit:]}

    def _run(self, request: dict[str, str]) -> tuple[RunExecution, DatasetInfo | DatasetBundleInfo]:
        dataset_id = request.get("dataset_id")
        if dataset_id is not None and not _operator_dataset(dataset_id):
            raise WebRequestError("Protected evaluation datasets are unavailable to operator runs")
        if request.get("input_kind") == "logistics_bundle":
            if self.logistics is None or not dataset_id:
                raise WebRequestError("Select a ready logistics bundle before running this question")
            dataset = self.logistics.dataset_info(dataset_id)
            contract = (self.config.task_contracts or {}).get("logistics-shipment-threshold-v1")
            if not contract:
                raise WebRequestError("Logistics task contract is unavailable")
            logistics_config = replace(self.config, task_family="logistics-shipment-threshold",
                task_contract_version="logistics-shipment-threshold-v1", contract_hash=canonical_hash(contract))
            active = self.history.active_version(logistics_config.task_family)
            if active:
                source = CandidateRepository(PROJECT_ROOT).active_checkout(active["commit"])
                workflow_revision_id = active.get("workflow_revision_id") or self._ensure_workflow(
                    task_family=logistics_config.task_family, source=source, source_commit=active["commit"])
                execution = CandidateRunner(
                    store=self.store, logistics=self.logistics, config=logistics_config,
                    history=self.history, telemetry=self.telemetry,
                    image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
                ).run(source=source, source_commit=active["commit"], dataset=dataset,
                      invocation=request["question"], model=self.model_factory(),
                      workflow_revision_id=workflow_revision_id)
                return execution, dataset
            baseline_commit = baseline_source_commit()
            baseline_source = CandidateRepository(PROJECT_ROOT).active_checkout(baseline_commit)
            workflow_revision_id = self._ensure_workflow(task_family=logistics_config.task_family,
                                                          source=baseline_source, source_commit=baseline_commit)
            execution = RunExecutor(history=self.history, telemetry=self.telemetry, config=logistics_config).run(
                model=self.model_factory(), tools=LogisticsTools(self.logistics.open_session(dataset_id), logistics_config),
                dataset=dataset, invocation=request["question"], agent_factory=LogisticsAgent,
                workflow_revision_id=workflow_revision_id, source_commit=baseline_commit)
            return execution, dataset
        dataset = self.store.dataset_info(dataset_id) if dataset_id is not None else self._default_dataset()
        active = self.history.active_version(self.config.task_family)
        if active:
            source = CandidateRepository(PROJECT_ROOT).active_checkout(active["commit"])
            workflow_revision_id = active.get("workflow_revision_id") or self._ensure_workflow(
                task_family=self.config.task_family, source=source, source_commit=active["commit"])
            execution = CandidateRunner(
                store=self.store, logistics=self.logistics, config=self.config, history=self.history, telemetry=self.telemetry,
                image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
            ).run(source=source, source_commit=active["commit"], dataset=dataset,
                  invocation=request["question"], model=self.model_factory(),
                  workflow_revision_id=workflow_revision_id)
            return execution, dataset
        table = self.store.open_session(dataset.dataset_id)
        baseline_commit = baseline_source_commit()
        baseline_source = CandidateRepository(PROJECT_ROOT).active_checkout(baseline_commit)
        workflow_revision_id = self._ensure_workflow(task_family=self.config.task_family,
                                                      source=baseline_source, source_commit=baseline_commit)
        execution = RunExecutor(history=self.history, telemetry=self.telemetry, config=self.config).run(
            model=self.model_factory(),
            tools=AnalystTools(table, self.config),
            dataset=dataset,
            invocation=request["question"],
            workflow_revision_id=workflow_revision_id,
            source_commit=baseline_commit,
        )
        return execution, dataset

    def _default_dataset(self) -> DatasetInfo:
        """Choose the deterministic operator dataset for a browser run.

        Keep generated evaluation tables out of automatic selection. Explicit
        API requests can still name a dataset. Resolve the chosen dataset again
        to verify its immutable metadata before use.
        """

        datasets = self.store.list_dataset_info()
        if not datasets:
            raise WebRequestError("No ready datasets are available")
        selected = next((item for item in datasets
                         if _operator_dataset(item.dataset_id)), None)
        if selected is None:
            raise WebRequestError("No operator dataset is available; seed one before running a question")
        return self.store.dataset_info(selected.dataset_id)


def _limit(query: dict[str, list[str]]) -> int:
    value = query.get("limit", ["20"])[0]
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WebRequestError("History limit must be an integer") from exc


def _cursor(query: dict[str, list[str]], name: str) -> int:
    value = query.get(name, ["0"])[0]
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WebRequestError("Evolution event cursor is invalid") from exc
    if parsed < 0:
        raise WebRequestError("Evolution event cursor is invalid")
    return parsed


def _run_request(body: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(body, dict) or not {"question"} <= set(body) <= {"dataset_id", "question", "input_kind"}:
        raise WebRequestError("A run needs question and an optional dataset_id")
    question = body["question"]
    if not isinstance(question, str) or not question.strip() or len(question) > 2_000:
        raise WebRequestError("Question must be 1 to 2000 characters")
    request = {"question": question.strip()}
    if "input_kind" in body:
        if body["input_kind"] not in {"inventory_table", "logistics_bundle"}:
            raise WebRequestError("Input kind is invalid")
        request["input_kind"] = body["input_kind"]
    if "dataset_id" in body:
        dataset_id = body["dataset_id"]
        if not isinstance(dataset_id, str) or not dataset_id.strip() or len(dataset_id) > 100:
            raise WebRequestError("Dataset ID is invalid")
        request["dataset_id"] = dataset_id.strip()
    return request


def create_server(application: WebApplication, host: str = "127.0.0.1", port: int = 4173) -> ThreadingHTTPServer:
    """Create, but do not start, the local server (useful for tests too)."""

    if not 1 <= port <= 65_535 and port != 0:
        raise ValueError("Port must be between 1 and 65535")

    class Handler(_WebRequestHandler):
        app = application

    return ThreadingHTTPServer((host, port), Handler)


class _WebRequestHandler(BaseHTTPRequestHandler):
    app: WebApplication
    server_version = "SelfHealLocalUI/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch_api_or_asset()

    def do_POST(self) -> None:  # noqa: N802
        route = urlsplit(self.path)
        if not route.path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_REQUEST_BYTES:
                raise WebRequestError("Request body must be between 1 and 8192 bytes")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise WebRequestError("Request content type must be application/json")
            body = json.loads(self.rfile.read(length))
            status, payload = self.app.api("POST", self.path, body)
            self._send_json(status, payload)
        except (DatasetError, WebRequestError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._server_error("Run could not be completed")

    def _dispatch_api_or_asset(self) -> None:
        route = urlsplit(self.path)
        if route.path.startswith("/api/"):
            try:
                status, payload = self.app.api("GET", self.path)
                self._send_json(status, payload)
            except (DatasetError, WebRequestError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._server_error("Service is unavailable")
            return
        self._send_asset(route.path)

    def _server_error(self, message: str) -> None:
        reference = uuid.uuid4().hex[:12]
        logging.getLogger(__name__).exception(
            "Local API %s %s failed (reference %s)",
            self.command, urlsplit(self.path).path, reference,
        )
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
            "error": f"{message}. Server reference: {reference}.",
            "error_id": reference,
        })

    def _send_asset(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path).lstrip("/")
        candidate = (ASSET_ROOT / relative).resolve()
        if ASSET_ROOT not in candidate.parents and candidate != ASSET_ROOT:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file() or candidate.suffix not in {".css", ".html", ".js", ".svg", ".woff2"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type if candidate.suffix == ".woff2" else f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int | HTTPStatus, payload: dict[str, Any]) -> None:
        rendered = json.dumps(payload, default=_json_default, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(rendered)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(rendered)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep routine browser requests out of the operator's terminal."""


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
