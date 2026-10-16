"""Daytona boundary and supervisor-owned model/table bridge for generated code."""

from __future__ import annotations

import os
import hashlib
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.agent import RunResult
from harness.logistics import TOOL_NAME, LogisticsTools, validate_logistics_task
from harness.tools import AnalystTools
from evals.analyst.oracle import OracleError, _validate_task
from self_heal.contracts import (
    build_run_completion_patch,
    build_run_start_record,
    new_run_id,
    trace_metadata,
    utc_now,
)
from self_heal.execution import RunExecution
from self_heal.model import ChatModel
from self_heal.settings import AnalystConfig
from self_heal.storage import HistoryStore
from self_heal.table_store import TableStore, DatasetInfo
from self_heal.logistics_store import DatasetBundleInfo, LogisticsDatasetStore
from self_heal.telemetry import PhoenixTelemetry
from self_heal.evidence import EvidenceTools, run_evidence


class RunnerError(RuntimeError):
    pass


class CandidateRunner:
    def __init__(
        self,
        *,
        store: TableStore | None,
        config: AnalystConfig,
        telemetry: PhoenixTelemetry,
        history: HistoryStore | None,
        image: str = "seal-python311-v1",
        repository: Path | None = None,
        logistics: LogisticsDatasetStore | None = None,
        daytona_client=None,
        transport_factory=None,
    ) -> None:
        self.store, self.logistics = store, logistics
        self.config, self.telemetry, self.history = config, telemetry, history
        self.image = image
        self._daytona_client = daytona_client
        self._snapshot = None
        from self_heal.daytona_transport import DaytonaTransport

        self.transport_factory = transport_factory or DaytonaTransport
        self.last_sandbox_id = None
        self.cleanup_error = None
        self.repository = (repository or Path(__file__).resolve().parents[2]).resolve()

    def dataset_info(
        self, dataset: DatasetInfo | DatasetBundleInfo
    ) -> DatasetInfo | DatasetBundleInfo:
        """Re-read immutable metadata through the store that owns this input."""

        input_kind = getattr(dataset, "input_kind", "inventory_table")
        if input_kind == "logistics_bundle":
            if self.logistics is None:
                raise RunnerError("Logistics candidate execution is not configured")
            return self.logistics.dataset_info(dataset.dataset_id)
        if input_kind == "inventory_table":
            if self.store is None:
                raise RunnerError("Inventory candidate execution is not configured")
            return self.store.dataset_info(dataset.dataset_id)
        raise RunnerError("Candidate input kind is not supported")

    def daytona(self):
        if self._daytona_client is None:
            from daytona import Daytona

            if not os.environ.get("DAYTONA_API_KEY"):
                raise RunnerError("DAYTONA_API_KEY must be set for isolated execution")
            self._daytona_client = Daytona()
        return self._daytona_client

    def snapshot(self):
        if self._snapshot is None:
            try:
                self._snapshot = self.daytona().snapshot.get(self.image)
            except Exception as exc:
                raise RunnerError(
                    "Daytona snapshot unavailable; run `seal runner build`"
                ) from exc
            state = str(
                getattr(self._snapshot.state, "value", self._snapshot.state)
            ).lower()
            if state != "active":
                self._snapshot = None
                raise RunnerError("Daytona snapshot is not active")
        return self._snapshot

    def image_identity(self) -> str:
        snapshot = self.snapshot()
        runtime_hash = hashlib.sha256()
        for path in sorted((self.repository / "runner_support").rglob("*.py")):
            runtime_hash.update(path.relative_to(self.repository).as_posix().encode())
            runtime_hash.update(path.read_bytes())
        return f"daytona:{snapshot.id}:{runtime_hash.hexdigest()}"

    def build_image(self) -> str:
        from daytona import CreateSnapshotParams, Resources
        from daytona.common.errors import DaytonaNotFoundError

        try:
            self._snapshot = self.daytona().snapshot.get(self.image)
        except DaytonaNotFoundError:
            self._snapshot = self.daytona().snapshot.create(
                CreateSnapshotParams(
                    name=self.image,
                    image="python:3.11-slim",
                    resources=Resources(cpu=1, memory=1, disk=3),
                ),
                timeout=300,
            )
        return self.image_identity()

    def run(
        self,
        *,
        source: Path,
        source_commit: str,
        dataset: DatasetInfo | DatasetBundleInfo,
        invocation: dict[str, Any] | str,
        model: ChatModel,
        case_id: str | None = None,
        case_exposure: str | None = None,
        workflow_revision_id: str | None = None,
    ) -> RunExecution:
        image_digest = self.image_identity()
        source = source.resolve()
        harness_dir = source / "harness"
        if not harness_dir.is_dir() or harness_dir.is_symlink():
            raise RunnerError("Candidate harness directory is unavailable")
        if any(path.is_symlink() for path in harness_dir.rglob("*")):
            raise RunnerError("Candidate harness contains a symlink")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if (
            head.returncode
            or dirty.returncode
            or head.stdout.strip() != source_commit
            or dirty.stdout.strip()
        ):
            raise RunnerError("Candidate source does not match its pinned clean commit")
        input_kind = getattr(dataset, "input_kind", "inventory_table")
        if input_kind == "logistics_bundle":
            if self.logistics is None:
                raise RunnerError("Logistics candidate execution is not configured")
            verified = self.dataset_info(dataset)
            table = self.logistics.open_session(dataset.dataset_id)
            audited_tools = EvidenceTools(LogisticsTools(table, self.config))
        elif input_kind == "inventory_table":
            if self.store is None:
                raise RunnerError("Inventory candidate execution is not configured")
            verified = self.dataset_info(dataset)
            table = self.store.open_session(dataset.dataset_id)
            audited_tools = EvidenceTools(AnalystTools(table, self.config))
        else:
            raise RunnerError("Candidate input kind is not supported")
        if verified != dataset:
            raise RunnerError("Dataset identity changed before execution")
        run_id, started_at = new_run_id(), utc_now()
        status = "not_configured" if self.history is None else "recording"
        history_error = None
        started = False
        if self.history:
            try:
                self.history.start_run(
                    build_run_start_record(
                        run_id=run_id,
                        invocation=invocation,
                        dataset=dataset,
                        config=self.config,
                        model=model,
                        started_at=started_at,
                        case_id=case_id,
                        case_exposure=case_exposure,
                        source_commit=source_commit,
                        runner_image_digest=image_digest,
                        workflow_revision_id=workflow_revision_id,
                    )
                )
                started = True
            except Exception as exc:
                status, history_error = "incomplete", type(exc).__name__

        result, trace = self.telemetry.execute(
            run_id=run_id,
            invocation=invocation,
            metadata=trace_metadata(
                run_id=run_id,
                dataset=dataset,
                invocation=invocation,
                config=self.config,
                model=model,
                source_commit=source_commit,
                runner_image_digest=image_digest,
            ),
            model=model,
            tools=audited_tools,
            started_at=started_at,
            execute=lambda traced_model, traced_tools: self._execute_sandbox(
                harness_dir,
                invocation,
                run_id,
                table,
                traced_model,
                traced_tools,
                input_kind=input_kind,
            ),
        )
        if self.history and started:
            try:
                completion = build_run_completion_patch(
                    result=result,
                    trace=trace,
                    completed_at=utc_now(),
                    evidence=run_evidence(table, audited_tools),
                )
                completion["sandbox"] = {
                    "provider": "daytona",
                    "id": self.last_sandbox_id,
                    "cleanup_status": "failed"
                    if self.cleanup_error
                    else "deleted"
                    if self.last_sandbox_id
                    else "not_created",
                    "cleanup_error": self.cleanup_error,
                }
                self.history.finish_run(run_id, completion)
                status = "recorded"
            except Exception as exc:
                status, history_error = "incomplete", type(exc).__name__
        return RunExecution(
            result=result,
            trace=trace,
            history_status=status,
            history_error=history_error,
        )

    def _execute_sandbox(
        self,
        harness_dir,
        invocation,
        run_id,
        table,
        model,
        tools,
        *,
        input_kind: str | None = None,
    ) -> RunResult:
        self.last_sandbox_id = None
        self.cleanup_error = None
        limits = self.config.limits
        input_kind = input_kind or getattr(table, "input_kind", "inventory_table")
        if input_kind not in {"inventory_table", "logistics_bundle"}:
            raise RunnerError("Candidate input kind is not supported")
        begun = time.monotonic()
        model_calls = tool_calls = tokens = observed_tools = 0
        payload = None
        logistics_results = []
        error = None
        try:
            with self.transport_factory(self, harness_dir) as transport:
                # Provisioning has its own timeout; task budgets start when execution starts.
                begun = time.monotonic()
                transport.send(
                    {
                        "run_id": run_id,
                        "invocation": invocation,
                        "input_kind": input_kind,
                        "config": {
                            "metrics": self.config.metrics,
                            "filter_fields": self.config.filter_fields,
                            "group_fields": self.config.group_fields,
                            "table_schema": self.config.table_schema,
                            "limits": asdict(limits),
                        },
                    }
                )
                while payload is None:
                    remaining = limits.max_elapsed_seconds - (time.monotonic() - begun)
                    if remaining <= 0:
                        raise RunnerError("Task time budget exceeded")
                    request = transport.receive(remaining)
                    kind = request.get("type")
                    try:
                        if kind == "model":
                            if model_calls >= limits.max_model_calls:
                                raise RunnerError("Model-call budget exceeded")
                            messages, definitions = (
                                request.get("messages"),
                                request.get("tools"),
                            )
                            if not isinstance(messages, list) or not isinstance(
                                definitions, list
                            ):
                                raise RunnerError("Invalid model request")
                            reply = model.complete(messages, definitions)
                            model_calls += 1
                            tool_calls += len(reply.tool_calls)
                            tokens += reply.total_tokens
                            if (
                                tool_calls > limits.max_tool_calls
                                or tokens > limits.max_total_tokens
                            ):
                                raise RunnerError("Model/tool/token budget exceeded")
                            value = {
                                "content": reply.content,
                                "total_tokens": reply.total_tokens,
                                "tool_calls": [
                                    asdict(call) for call in reply.tool_calls
                                ],
                            }
                        elif kind == "table":
                            if input_kind != "inventory_table":
                                raise RunnerError(
                                    "Inventory table access is unavailable for this input"
                                )
                            operation, arguments = (
                                request.get("operation"),
                                request.get("arguments"),
                            )
                            if not isinstance(arguments, dict):
                                raise RunnerError("Invalid table request")
                            if operation == "inspect_table" and not arguments:
                                value = table.inspect_table()
                            elif operation == "read_rows" and set(arguments) <= {
                                "cursor",
                                "limit",
                                "filter_field",
                                "filter_value",
                            }:
                                value = table.read_rows(**arguments)
                            elif operation == "completed_scan" and set(arguments) == {
                                "filter_field",
                                "filter_value",
                            }:
                                value = table.completed_scan(**arguments)
                            else:
                                raise RunnerError("Table operation is not permitted")
                        elif kind == "logistics":
                            if input_kind != "logistics_bundle":
                                raise RunnerError(
                                    "Logistics access is unavailable for this input"
                                )
                            operation, arguments = (
                                request.get("operation"),
                                request.get("arguments"),
                            )
                            if not isinstance(arguments, dict):
                                raise RunnerError("Invalid logistics request")
                            if operation == "inspect_catalog" and not arguments:
                                value = table.inspect_catalog()
                            elif (
                                operation == "inspect_relation"
                                and set(arguments) == {"relation"}
                                and isinstance(arguments["relation"], str)
                            ):
                                value = table.inspect_relation(**arguments)
                            elif operation == "read_shipments" and set(arguments) <= {
                                "warehouse_number",
                                "relative_day",
                                "limit",
                                "cursor",
                            }:
                                value = table.read_shipments(**arguments)
                            else:
                                raise RunnerError(
                                    "Logistics operation is not permitted"
                                )
                        elif kind == "tool_result":
                            observed_tools += 1
                            if input_kind == "logistics_bundle":
                                tool_calls = observed_tools
                                if tool_calls > limits.max_tool_calls:
                                    raise RunnerError("Tool-call budget exceeded")
                            elif observed_tools > tool_calls:
                                raise RunnerError("Unmatched candidate tool envelope")
                            name, arguments, value = (
                                request.get("name"),
                                request.get("arguments"),
                                request.get("result"),
                            )
                            if (
                                not isinstance(name, str)
                                or not isinstance(arguments, dict)
                                or not isinstance(value, dict)
                            ):
                                raise RunnerError("Invalid tool envelope")
                            if input_kind == "logistics_bundle" and name == TOOL_NAME and "value" in value:
                                logistics_results.append((arguments, value))
                            record = getattr(tools, "record_remote_tool", None)
                            if callable(record):
                                record(name, arguments, value)
                            value = None
                        elif kind == "result":
                            payload = request.get("value")
                            value = None
                        else:
                            raise RunnerError("Unknown candidate bridge request")
                        transport.send({"ok": True, "value": value})
                    except Exception as exc:
                        transport.send({"ok": False, "error": str(exc)[:200]})
                        if isinstance(exc, RunnerError):
                            raise
        except Exception as exc:
            error = (
                f"Isolated candidate failure: {type(exc).__name__}: {str(exc)[:120]}"
            )

        elapsed = round(time.monotonic() - begun, 3)
        if not isinstance(payload, dict):
            payload = {}
        outcome = payload.get("outcome")
        answer = payload.get("answer")
        interpreted = payload.get("interpreted_task")
        if error is None and outcome == "answered":
            if not isinstance(answer, dict) or not isinstance(interpreted, dict):
                error = "Candidate returned an invalid answer envelope"
            else:
                if input_kind == "logistics_bundle":
                    expected = validate_logistics_task(invocation if isinstance(invocation, dict) else interpreted)
                    if isinstance(invocation, dict) and (expected is None or interpreted != expected):
                        error = "Candidate returned a task outside the protected logistics contract"
                    if error is None and not _valid_answer(answer, grouped="groups" in answer):
                        error = "Candidate returned an invalid answer shape"
                    if error is None and expected is not None and not any(
                        arguments == {key: expected[key] for key in ("warehouse_number", "relative_day", "threshold")}
                        and answer == {"value": value["value"]}
                        for arguments, value in logistics_results
                    ):
                        error = "Candidate answer lacks matching observed logistics tool evidence"
                    if error is None and observed_tools == 0:
                        error = "Candidate did not execute an observed logistics tool"
                    if error is None and expected is not None and not table.completed_shipment_scan(
                        interpreted["warehouse_number"],
                        interpreted["relative_day"],
                    ):
                        error = "Required shipment rows were not fully read"
                else:
                    try:
                        if isinstance(invocation, dict) or "metric" in interpreted:
                            _validate_task(interpreted, self.config)
                    except OracleError:
                        error = (
                            "Candidate returned a task outside the protected contract"
                        )
                    if (
                        error is None
                        and isinstance(invocation, dict)
                        and interpreted != invocation
                    ):
                        error = "Candidate changed the requested structured task"
                    if error is None and not _valid_answer(
                        answer, grouped="groups" in answer
                    ):
                        error = "Candidate returned an invalid answer shape"
                    if error is None and not table.completed_scan(
                        interpreted.get("filter_field"), interpreted.get("filter_value")
                    ):
                        error = "Required table rows were not fully read"
        if outcome not in {"answered", "unsupported", "error"}:
            error = error or "Candidate returned an invalid outcome"
        if elapsed > limits.max_elapsed_seconds:
            error = "Task time budget exceeded"
        return RunResult(
            run_id=run_id,
            answer=answer if not error and isinstance(answer, dict) else None,
            error=error or (payload.get("error") if outcome == "error" else None),
            outcome="error" if error else outcome,
            interpreted_task=interpreted if isinstance(interpreted, dict) else None,
            model_calls=model_calls,
            tool_calls=tool_calls,
            total_tokens=tokens,
            elapsed_seconds=elapsed,
            table_pages=table.pages_read,
            table_bytes=table.bytes_read,
            limitation_kind=payload.get("limitation_kind")
            if outcome == "unsupported"
            else None,
            limitation_reason=payload.get("limitation_reason")
            if outcome == "unsupported"
            else None,
            capability_request=payload.get("capability_request") if isinstance(payload.get("capability_request"), dict) else None,
        )


def _valid_answer(answer: dict[str, Any], *, grouped: bool) -> bool:
    if grouped:
        groups = answer.get("groups")
        return (
            set(answer) == {"groups"}
            and isinstance(groups, dict)
            and list(groups) == sorted(groups)
            and all(
                isinstance(key, str) and type(value) is int
                for key, value in groups.items()
            )
        )
    return set(answer) == {"value"} and type(answer.get("value")) is int
