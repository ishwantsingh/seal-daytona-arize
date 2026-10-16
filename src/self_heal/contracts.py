"""Stable evidence contracts owned by the trusted supervisor.

The editable harness returns a small ``RunResult``.  This module turns that
result into compact, versioned records without copying table rows or detailed
Arize Phoenix payloads into SQLite.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from harness.agent import RunResult
from self_heal.model import ChatModel, OpenRouterModel
from self_heal.settings import AnalystConfig
from self_heal.table_store import DatasetInfo


SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    return str(uuid.uuid4())


def new_evaluation_id() -> str:
    return str(uuid.uuid4())


def new_trial_id() -> str:
    return str(uuid.uuid4())


def new_candidate_id() -> str:
    return str(uuid.uuid4())


def new_version_id() -> str:
    return str(uuid.uuid4())


def canonical_hash(value: Any) -> str:
    """Hash ordinary JSON data with stable key ordering."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_case_id(
    *, scenario_id: str, dataset: DatasetInfo, task: Mapping[str, Any], oracle_version: str
) -> str:
    return "case_" + canonical_hash(
        {
            "scenario_id": scenario_id,
            "dataset_id": dataset.dataset_id,
            "dataset_hash": dataset.content_hash,
            "task": dict(task),
            "oracle_version": oracle_version,
        }
    )[:32]


def invocation_record(invocation: dict[str, Any] | str, config: AnalystConfig) -> dict[str, Any]:
    if isinstance(invocation, str):
        task_id = "task_" + canonical_hash(
            {"question": invocation, "family": config.task_family, "version": config.task_contract_version}
        )[:32]
        return {
            "kind": "question",
            "task_id": task_id,
            "question": invocation,
            "question_hash": canonical_hash(invocation),
            "requested_task": None,
            "task_family": config.task_family,
            "task_contract_version": config.task_contract_version,
        }
    requested_task = dict(invocation)
    return {
        "kind": "task",
        "task_id": "task_"
        + canonical_hash(
            {"task": requested_task, "family": config.task_family, "version": config.task_contract_version}
        )[:32],
        "question": None,
        "question_hash": None,
        "requested_task": requested_task,
        "task_family": config.task_family,
        "task_contract_version": config.task_contract_version,
    }


def dataset_record(dataset: DatasetInfo) -> dict[str, Any]:
    record = {
        "id": dataset.dataset_id,
        "content_hash": dataset.content_hash,
        "row_count": dataset.row_count,
    }
    # Logistics bundles use a richer immutable manifest while inventory tables
    # retain their compact backwards-compatible evidence shape.
    if hasattr(dataset, "input_kind"):
        record.update({
            "input_kind": dataset.input_kind,
            "domain": getattr(dataset, "domain", None),
            "schema_version": getattr(dataset, "schema_version", None),
            "relations": getattr(dataset, "relations", None),
        })
    return record


def model_identity(model: ChatModel) -> dict[str, Any]:
    if isinstance(model, OpenRouterModel):
        provider = "openrouter"
        model_id = model.model
        settings = model.tracing_settings
    else:
        provider = "scripted"
        model_id = getattr(model, "model", model.__class__.__name__)
        settings = {}
    return {
        "provider": provider,
        "id": str(model_id),
        "settings": settings,
        "settings_hash": canonical_hash(settings),
    }


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_stdout(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


@lru_cache(maxsize=1)
def source_identity() -> dict[str, Any]:
    """A secret-free source/runtime fingerprint that also works outside Git."""

    commit = _git_stdout("rev-parse", "HEAD") or "unknown"
    status = _git_stdout("status", "--porcelain")
    dirty = bool(status) if status is not None else None
    patch_hash = None
    if dirty:
        patch = _git_stdout("diff", "--binary", "--no-ext-diff")
        patch_hash = canonical_hash(patch or status or "")
    return {
        "source": {
            "commit": commit,
            "dirty": dirty,
            "dirty_patch_hash": patch_hash,
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
            "uv_lock_hash": _hash_file(PROJECT_ROOT / "uv.lock"),
        },
    }


def execution_identity(config: AnalystConfig, model: ChatModel) -> dict[str, Any]:
    identity = source_identity()
    return {
        **identity,
        "config": {
            "sha256": config.config_hash,
            "contract_sha256": config.contract_hash,
            "task_contract_version": config.task_contract_version,
        },
        "model": model_identity(model),
    }


def trace_metadata(
    *, run_id: str, dataset: DatasetInfo, invocation: dict[str, Any] | str, config: AnalystConfig, model: ChatModel,
    source_commit: str | None = None,
    runner_image_digest: str | None = None,
) -> dict[str, Any]:
    """Metadata suitable for a root trace; the telemetry redactor is the final gate."""

    record = invocation_record(invocation, config)
    model_record = model_identity(model)
    return {
        "run_id": run_id,
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.content_hash,
        "dataset_row_count": dataset.row_count,
        "task_family": config.task_family,
        "task_contract_version": config.task_contract_version,
        "config_sha256": config.config_hash,
        "contract_sha256": config.contract_hash,
        "model_id": model_record["id"],
        "model_settings_sha256": model_record["settings_hash"],
        "source_commit": source_commit or source_identity()["source"]["commit"],
        "runner_image_digest": runner_image_digest,
        "question": record["question"],
        "question_hash": record["question_hash"],
    }


def build_run_start_record(
    *,
    run_id: str,
    invocation: dict[str, Any] | str,
    dataset: DatasetInfo,
    config: AnalystConfig,
    model: ChatModel,
    started_at: datetime,
    case_id: str | None = None,
    case_exposure: str | None = None,
    source_commit: str | None = None,
    runner_image_digest: str | None = None,
    workflow_revision_id: str | None = None,
) -> dict[str, Any]:
    execution = execution_identity(config, model)
    if source_commit is not None:
        execution["source"] = {"commit": source_commit, "dirty": False, "dirty_patch_hash": None}
    if runner_image_digest is not None:
        execution["runtime"] = {"isolation": "daytona", "image_digest": runner_image_digest}
    record = {
        "_id": run_id,
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "created_at": started_at,
        "invocation": invocation_record(invocation, config),
        "dataset": dataset_record(dataset),
        "case": {"case_id": case_id, "exposure_role": case_exposure} if case_id else None,
        "execution": execution,
        "lifecycle": [{"state": "started", "at": started_at}],
    }
    if workflow_revision_id is not None:
        record["workflow_revision_id"] = workflow_revision_id
    return record


def resource_summary(result: RunResult) -> dict[str, Any]:
    return {
        "model_calls": result.model_calls,
        "tool_calls": result.tool_calls,
        "total_tokens": result.total_tokens,
        "elapsed_seconds": result.elapsed_seconds,
        "table_pages": result.table_pages,
        "table_bytes": result.table_bytes,
    }


def build_run_completion_patch(
    *, result: RunResult, trace: Any, completed_at: datetime, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the immutable outcome portion of a compact SQLite run record."""

    trace_record = {
        "provider": "phoenix",
        "project": trace.project,
        "root_id": trace.trace_id,
        "url": trace.url,
        "status": trace.status,
        "started_at": trace.started_at,
        "checked_at": trace.checked_at,
        "error_type": trace.error_type,
        "redaction_version": trace.redaction_version,
    }
    if trace_record["root_id"] is None:
        trace_record.pop("root_id")
    return {
        "status": "completed",
        "completed_at": completed_at,
        "outcome": result.outcome,
        "answer": dict(result.answer) if result.answer is not None else None,
        "error": result.error,
        "interpreted_task": dict(result.interpreted_task) if result.interpreted_task is not None else None,
        "limitation_kind": result.limitation_kind,
        "limitation_reason": result.limitation_reason,
        "capability_request": dict(result.capability_request) if result.capability_request else None,
        "resources": resource_summary(result),
        "trace": trace_record,
        "evidence": evidence,
        "lifecycle_entry": {"state": "completed", "at": completed_at},
    }


def build_eval_case_record(
    *,
    case_id: str,
    scenario_id: str,
    dataset: DatasetInfo,
    task: Mapping[str, Any],
    expected_answer: Mapping[str, Any],
    oracle_version: str,
    config: AnalystConfig,
    exposure_role: str,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "_id": case_id,
        "case_id": case_id,
        "schema_version": SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "task": dict(task),
        "task_family": config.task_family,
        "task_contract_version": config.task_contract_version,
        "dataset": dataset_record(dataset),
        "oracle": {
            "version": oracle_version,
            "expected_answer_hash": canonical_hash(dict(expected_answer)),
        },
        "origin": "declared_scenario",
        "created_at": created_at,
        "exposure": {"role": exposure_role, "at": created_at, "source": "phase-2-scenario"},
    }


def build_evaluation_record(
    *,
    evaluation_id: str,
    case_id: str,
    result: RunResult,
    passed: bool,
    violation: str | None,
    dataset: DatasetInfo,
    trace: Any | None,
    config: AnalystConfig,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "_id": evaluation_id,
        "evaluation_id": evaluation_id,
        "trial_id": evaluation_id,
        "schema_version": SCHEMA_VERSION,
        "run_id": result.run_id,
        "case_id": case_id,
        "task_family": config.task_family,
        "dataset": dataset_record(dataset),
        "actual": {
            "outcome": result.outcome,
            "answer": dict(result.answer) if result.answer is not None else None,
            "error": result.error,
        },
        "passed": passed,
        "violation": violation,
        "resources": resource_summary(result),
        "trace_id": trace.trace_id if trace is not None else None,
        "created_at": created_at,
    }
