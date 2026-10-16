"""One supervisor-owned execution path for interactive and evaluation runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.agent import AnalystAgent, RunResult
from harness.tools import AnalystTools
from self_heal.contracts import (
    build_run_completion_patch,
    build_run_start_record,
    new_run_id,
    trace_metadata,
    utc_now,
)
from self_heal.evidence import EvidenceTools, run_evidence
from self_heal.model import ChatModel
from self_heal.repository import CandidateRepository
from self_heal.settings import AnalystConfig
from self_heal.storage import HistoryStore
from self_heal.table_store import DatasetInfo
from self_heal.telemetry import PhoenixTelemetry, TraceEvidence

# Capture the baseline at process startup, alongside the imported harness. Later
# disk edits must not be attributed to code already loaded by this process.
_BASELINE_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_HARNESS = {path.name: path.read_text() for path in sorted((_BASELINE_ROOT / "harness").glob("*.py"))}


def baseline_source_commit() -> str:
    return CandidateRepository(_BASELINE_ROOT).snapshot_harness(_BASELINE_HARNESS)


@dataclass(frozen=True)
class RunExecution:
    result: RunResult
    trace: TraceEvidence
    history_status: str
    history_error: str | None = None

    def compact_evidence(self) -> dict[str, Any]:
        return {
            "trace": self.trace.to_dict(),
            "history": {"status": self.history_status, "error_type": self.history_error},
        }


class RunExecutor:
    """Coordinates exact IDs across the agent, Arize Phoenix, and SQLite history."""

    def __init__(
        self,
        *,
        history: HistoryStore | None,
        telemetry: PhoenixTelemetry,
        config: AnalystConfig,
    ) -> None:
        self.history = history
        self.telemetry = telemetry
        self.config = config

    def run(
        self,
        *,
        model: ChatModel,
        tools: AnalystTools,
        dataset: DatasetInfo,
        invocation: dict[str, Any] | str,
        case_id: str | None = None,
        case_exposure: str | None = None,
        workflow_revision_id: str | None = None,
        agent_factory: Callable[[ChatModel, Any, AnalystConfig], Any] = AnalystAgent,
        source_commit: str | None = None,
    ) -> RunExecution:
        source_commit = source_commit or baseline_source_commit()
        run_id = new_run_id()
        started_at = utc_now()
        history_status = "not_configured" if self.history is None else "recording"
        history_error: str | None = None
        history_started = False
        if self.history is not None:
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
                        workflow_revision_id=workflow_revision_id,
                        source_commit=source_commit,
                    )
                )
                history_started = True
            except Exception as exc:
                history_status = "incomplete"
                history_error = type(exc).__name__

        def invoke(traced_model: ChatModel, traced_tools: AnalystTools) -> RunResult:
            return agent_factory(traced_model, traced_tools, self.config).run(invocation, run_id=run_id)

        audited_tools = EvidenceTools(tools)
        result, trace = self.telemetry.execute(
            run_id=run_id,
            invocation=invocation,
            metadata=trace_metadata(
                run_id=run_id,
                dataset=dataset,
                invocation=invocation,
                config=self.config,
                model=model,
            ),
            model=model,
            tools=audited_tools,
            execute=invoke,
            started_at=started_at,
        )
        if self.history is not None and history_started:
            try:
                self.history.finish_run(
                    run_id,
                    build_run_completion_patch(result=result, trace=trace, completed_at=utc_now(),
                                               evidence=run_evidence(tools.table, audited_tools)),
                )
                history_status = "recorded"
            except Exception as exc:
                history_status = "incomplete"
                history_error = type(exc).__name__
        return RunExecution(
            result=result,
            trace=trace,
            history_status=history_status,
            history_error=history_error,
        )
