"""Thin, trusted Arize Phoenix instrumentation for a fixed model/tool harness.

SQLite keeps compact supervisor evidence. Arize Phoenix receives detailed nested
traces only after this module redacts secrets, table rows, and aggregate
answers. A tracing failure never changes the agent's measured outcome.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from harness.tools import AnalystTools
from self_heal.model import ChatModel, ModelReply
from self_heal.settings import PhoenixConfig


REDACTION_VERSION = "v1"
_SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "connection_string",
)
_URI_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE
)
_KEY_LIKE_VALUE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,})\b")


def redact_trace_payload(payload: Any) -> Any:
    """Recursively remove secrets and raw table/answer values before upload.

    Wrapped OpenAI calls include messages as JSON strings, so those strings are
    parsed when possible before being returned to Arize Phoenix.
    """

    return _redact(payload, parent_key=None)


def _redact(value: Any, *, parent_key: str | None) -> Any:
    key = parent_key.lower() if parent_key else ""
    if any(part in key for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if key == "rows":
        return _row_summary(value)
    if key == "row":
        return _single_row_summary(value)
    if key == "answer":
        return _answer_summary(value)
    if isinstance(value, dict):
        if set(value) == {"value"} and isinstance(value.get("value"), (int, float)):
            return {"value": "[REDACTED]"}
        if set(value) == {"groups"} and isinstance(value.get("groups"), dict):
            return {"groups": {name: "[REDACTED]" for name in value["groups"]}}
        return {
            str(item_key): _redact(item, parent_key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _row_summary(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {"redacted_row_count": None, "fields": []}
    fields = sorted({field for row in rows if isinstance(row, dict) for field in row})
    return {"redacted_row_count": len(rows), "fields": fields}


def _single_row_summary(row: Any) -> dict[str, Any]:
    return {
        "redacted_row": True,
        "fields": sorted(row) if isinstance(row, dict) else [],
    }


def _answer_summary(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        return {"redacted_answer_fields": sorted(answer)}
    return {"redacted_answer": answer is not None}


def _redact_string(value: str) -> str:
    # Tool results are serialized into OpenAI message content. Redact their
    # nested rows and final answers while retaining the shape of the exchange.
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, (dict, list)):
        cleaned = _redact(decoded, parent_key=None)
        if cleaned != decoded:
            return json.dumps(
                cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
    value = _URI_CREDENTIALS.sub(r"\g<scheme>[REDACTED]@", value)
    return _KEY_LIKE_VALUE.sub("[REDACTED]", value)


@dataclass(frozen=True)
class TraceEvidence:
    trace_id: str | None
    url: str | None
    status: str
    project: str | None
    started_at: datetime | None
    checked_at: datetime | None
    error_type: str | None = None
    redaction_version: str = REDACTION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.trace_id,
            "url": self.url,
            "status": self.status,
            "project": self.project,
            "started_at": self.started_at,
            "checked_at": self.checked_at,
            "error_type": self.error_type,
            "redaction_version": self.redaction_version,
        }


class TraceFactory(Protocol):
    def __call__(self, name: str, run_type: str = "chain", **kwargs: Any) -> Any: ...


class PhoenixClientFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


class TracedModel:
    """Model proxy that gives scripted and OpenRouter models a logical child span."""

    def __init__(
        self,
        model: ChatModel,
        *,
        trace_factory: TraceFactory,
        client: Any,
        project: str,
        metadata: dict[str, Any],
        mark_incomplete: Callable[[Exception], None],
    ) -> None:
        self._model = model
        self._trace_factory = trace_factory
        self._client = client
        self._project = project
        self._metadata = metadata
        self._mark_incomplete = mark_incomplete

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        started_model = False
        completed_model = False
        reply: ModelReply | None = None
        try:
            context = self._trace_factory(
                "analyst.model",
                run_type="chain",
                client=self._client,
                project_name=self._project,
                inputs={"messages": messages, "tools": tools},
                metadata=self._metadata,
                enabled=True,
            )
            with context as span:
                started_model = True
                reply = self._model.complete(messages, tools)
                completed_model = True
                _end_span(
                    span,
                    {
                        "content": reply.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                            for call in reply.tool_calls
                        ],
                        "total_tokens": reply.total_tokens,
                    },
                )
                return reply
        except Exception as exc:
            if completed_model:
                self._mark_incomplete(exc)
                assert reply is not None
                return reply
            if not started_model:
                self._mark_incomplete(exc)
                return self._model.complete(messages, tools)
            raise


class TracedTools:
    """Delegating tool proxy that preserves the harness's exact tool surface."""

    def __init__(
        self,
        tools: AnalystTools,
        *,
        trace_factory: TraceFactory,
        client: Any,
        project: str,
        metadata: dict[str, Any],
        mark_incomplete: Callable[[Exception], None],
    ) -> None:
        self._tools = tools
        self._trace_factory = trace_factory
        self._client = client
        self._project = project
        self._metadata = metadata
        self._mark_incomplete = mark_incomplete

    @property
    def table(self) -> Any:
        return self._tools.table

    def definitions(self) -> list[dict[str, Any]]:
        return self._tools.definitions()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started_tool = False
        completed_tool = False
        result: dict[str, Any] | None = None
        try:
            context = self._trace_factory(
                f"tool.{name}",
                run_type="tool",
                client=self._client,
                project_name=self._project,
                inputs={"name": name, "arguments": arguments},
                metadata=self._metadata,
                enabled=True,
            )
            with context as span:
                started_tool = True
                result = self._tools.execute(name, arguments)
                completed_tool = True
                _end_span(span, {"result": result})
                return result
        except Exception as exc:
            if completed_tool:
                self._mark_incomplete(exc)
                assert result is not None
                return result
            if not started_tool:
                self._mark_incomplete(exc)
                return self._tools.execute(name, arguments)
            raise

    def record_remote_tool(
        self, name: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Record an isolated candidate's tool envelope without executing it again."""
        record = getattr(self._tools, "record_remote_tool", None)
        if callable(record):
            record(name, arguments, result)
        try:
            context = self._trace_factory(
                f"tool.{name}",
                run_type="tool",
                client=self._client,
                project_name=self._project,
                inputs={"name": name, "arguments": arguments},
                metadata=self._metadata,
                enabled=True,
            )
            with context as span:
                _end_span(span, {"result": result})
        except Exception as exc:
            self._mark_incomplete(exc)


def _end_span(span: Any, outputs: dict[str, Any]) -> None:
    end = getattr(span, "end", None)
    if callable(end):
        end(outputs=outputs)


def _add_metadata(span: Any, metadata: dict[str, Any]) -> None:
    add = getattr(span, "add_metadata", None)
    if callable(add):
        add(metadata)


def _add_tags(span: Any, tags: list[str]) -> None:
    add = getattr(span, "add_tags", None)
    if callable(add):
        add(tags)


class _PhoenixSpan:
    """OpenInference span facade; all payloads are redacted before export."""

    def __init__(self, tracer, name, run_type="chain", **kwargs):
        self.context = tracer.start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        )
        self.inputs = kwargs.get("inputs", {})
        self.metadata = kwargs.get("metadata", {})
        self.kind = (
            "LLM"
            if name == "analyst.model"
            else "TOOL"
            if run_type == "tool"
            else "CHAIN"
        )

    def __enter__(self):
        self.span = self.context.__enter__()
        self.span.set_attribute("openinference.span.kind", self.kind)
        self.span.set_attribute(
            "input.value", json.dumps(redact_trace_payload(self.inputs), default=str)
        )
        self.span.set_attribute("input.mime_type", "application/json")
        self.add_metadata(self.metadata)
        if self.kind == "LLM":
            self.span.set_attribute(
                "llm.model_name", str(self.metadata.get("model_id", "unknown"))
            )
        return self

    def __exit__(self, exc_type, exc, tb):
        from opentelemetry.trace import Status, StatusCode

        self.span.set_status(Status(StatusCode.ERROR if exc else StatusCode.OK))
        if exc:
            self.span.set_attribute("seal.error_type", type(exc).__name__)
        return self.context.__exit__(None, None, None)

    def add_metadata(self, metadata):
        self.metadata = {**self.metadata, **metadata}
        self.span.set_attribute(
            "metadata", json.dumps(redact_trace_payload(self.metadata), default=str)
        )
        if "run_id" in metadata:
            self.span.set_attribute("seal.run_id", str(metadata["run_id"]))

    def add_tags(self, tags):
        self.span.set_attribute("tag.tags", tags)

    def end(self, *, outputs):
        self.span.set_attribute(
            "output.value", json.dumps(redact_trace_payload(outputs), default=str)
        )
        self.span.set_attribute("output.mime_type", "application/json")
        if outputs.get("total_tokens") is not None:
            self.span.set_attribute("llm.token_count.total", outputs["total_tokens"])


class PhoenixTelemetry:
    """Arize Phoenix tracing, verified retrieval, and deterministic evaluations."""

    def __init__(
        self,
        config: PhoenixConfig,
        *,
        provider=None,
        client=None,
        verification_attempts=3,
        retry_delay_seconds=0.5,
    ):
        self._config, self._provider, self._client = config, provider, client
        self._verification_attempts = verification_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._lock = __import__("threading").RLock()

    def _dependencies(self):
        with self._lock:
            if self._provider is None:
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                endpoint = self._config.collector_endpoint.rstrip("/")
                if not endpoint.endswith("/v1/traces"):
                    endpoint += "/v1/traces"
                headers = (
                    {"Authorization": "Bearer " + self._config.api_key}
                    if self._config.api_key
                    else {}
                )
                self._provider = TracerProvider(
                    resource=Resource.create(
                        {
                            "service.name": "seal",
                            "openinference.project.name": self._config.project,
                        }
                    )
                )
                self._provider.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=10)
                    )
                )
            if self._client is None:
                from phoenix.client import Client

                self._client = Client(
                    base_url=self._config.base_url, api_key=self._config.api_key
                )
            return self._provider, self._client

    def _spans(self, trace_id):
        if not self._config.enabled:
            raise LookupError("Phoenix tracing is disabled")
        _, client = self._dependencies()
        spans = client.spans.get_spans(
            project_identifier=self._config.project, trace_ids=[trace_id], limit=1000
        )
        if not spans or not any(not span.get("parent_id") for span in spans):
            raise LookupError("Phoenix incident trace is unavailable")
        return spans

    def read_redacted_trace(self, trace_id):
        return [
            redact_trace_payload(
                {
                    "name": span["name"],
                    "run_type": span.get("span_kind"),
                    "inputs": span.get("attributes", {}).get("input.value"),
                    "outputs": span.get("attributes", {}).get("output.value"),
                    "error": span.get("status_message"),
                }
            )
            for span in self._spans(trace_id)
        ]

    def trace_spans(self, trace_id):
        from self_heal.evidence import safe_payload

        results = []
        for span in self._spans(trace_id):
            attrs = span.get("attributes", {})
            start = datetime.fromisoformat(span["start_time"].replace("Z", "+00:00"))
            end = (
                datetime.fromisoformat(span["end_time"].replace("Z", "+00:00"))
                if span.get("end_time")
                else None
            )

            def parsed(key):
                raw = attrs.get(key, {})
                try:
                    return json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    return {}

            results.append(
                {
                    "id": span["context"]["span_id"],
                    "parent_id": span.get("parent_id"),
                    "name": span["name"],
                    "type": str(span.get("span_kind", "chain")).lower(),
                    "start_time": start,
                    "duration_ms": round((end - start).total_seconds() * 1000, 1)
                    if end
                    else None,
                    "status": "error"
                    if span.get("status_code") == "ERROR"
                    else "completed",
                    "error": redact_trace_payload(span.get("status_message")),
                    "model": attrs.get("llm.model_name"),
                    "tokens": attrs.get("llm.token_count.total"),
                    **(
                        {
                            "arguments": safe_payload(
                                parsed("input.value").get("arguments")
                            ),
                            "result_preview": redact_trace_payload(
                                parsed("output.value").get("result")
                            ),
                        }
                        if span["name"].startswith("tool.")
                        else {}
                    ),
                }
            )
        return sorted(results, key=lambda s: str(s["start_time"]))

    def execute(
        self, *, run_id, invocation, metadata, model, tools, execute, started_at
    ):
        if not self._config.enabled:
            return execute(model, tools), TraceEvidence(
                None, None, "disabled", self._config.project, None, None
            )
        trace_id = None
        try:
            provider, client = self._dependencies()
            tracer = provider.get_tracer("seal.harness")
        except Exception as exc:
            return execute(model, tools), self._incomplete(trace_id, started_at, exc)
        failures = []
        factory = lambda name, run_type="chain", **kwargs: _PhoenixSpan(
            tracer, name, run_type, **kwargs
        )
        traced_model = TracedModel(
            model,
            trace_factory=factory,
            client=client,
            project=self._config.project,
            metadata=metadata,
            mark_incomplete=failures.append,
        )
        traced_tools = TracedTools(
            tools,
            trace_factory=factory,
            client=client,
            project=self._config.project,
            metadata=metadata,
            mark_incomplete=failures.append,
        )
        # The agent is invoked once. Export/retrieval failures cannot replay side effects.
        result = None
        began = False
        try:
            with factory(
                "analyst.run",
                inputs={"invocation": invocation},
                metadata={**metadata, "run_id": run_id},
            ) as root:
                began = True
                trace_id = format(root.span.get_span_context().trace_id, "032x")
                result = execute(traced_model, traced_tools)
                root.add_metadata(
                    {
                        "outcome": result.outcome,
                        "limitation_kind": result.limitation_kind,
                        "resources": {
                            "model_calls": result.model_calls,
                            "tool_calls": result.tool_calls,
                            "total_tokens": result.total_tokens,
                            "elapsed_seconds": result.elapsed_seconds,
                        },
                    }
                )
                root.end(
                    outputs={
                        "outcome": result.outcome,
                        "error": result.error,
                        "answer": result.answer,
                        "limitation_reason": result.limitation_reason,
                    }
                )
        except Exception as exc:
            failures.append(exc)
            if result is None:
                if began:
                    raise
                result = execute(model, tools)
        try:
            if not provider.force_flush(timeout_millis=10000):
                raise TimeoutError("Phoenix export did not flush")
            for attempt in range(self._verification_attempts):
                try:
                    self._spans(trace_id)
                    break
                except Exception:
                    if attempt + 1 == self._verification_attempts:
                        raise
                    time.sleep(self._retry_delay_seconds)
            if failures:
                raise failures[0]
            project = client.projects.get(project_name=self._config.project)
            url = f"{self._config.base_url}/projects/{project['id']}/traces/{trace_id}"
            evidence = TraceEvidence(
                trace_id,
                url,
                "available",
                self._config.project,
                started_at,
                datetime.now(started_at.tzinfo),
            )
        except Exception as exc:
            evidence = self._incomplete(trace_id, started_at, exc)
        return result, evidence

    def publish_evaluation(self, record):
        if not self._config.enabled:
            return {"status": "disabled"}
        trace_id = record.get("trace_id")
        if not trace_id:
            return {"status": "incomplete", "error_type": "MissingTrace"}
        try:
            _, client = self._dependencies()
            spans = self._spans(trace_id)
            root = next(span for span in spans if not span.get("parent_id"))
            client.spans.add_span_annotation(
                span_id=root["context"]["span_id"],
                annotation_name="correctness",
                annotator_kind="CODE",
                score=float(record["passed"]),
                label="pass" if record["passed"] else "fail",
                explanation=record.get("violation")
                or "Independent protected oracle passed",
                identifier=record["evaluation_id"],
                metadata={
                    "case_id": record["case_id"],
                    "role": record.get("role"),
                    "run_id": record["run_id"],
                },
                sync=True,
            )
            return {"status": "available", "trace_id": trace_id}
        except Exception as exc:
            return {"status": "incomplete", "error_type": type(exc).__name__}

    def _incomplete(self, trace_id, started_at, error):
        return TraceEvidence(
            trace_id,
            None,
            "incomplete",
            self._config.project,
            started_at,
            datetime.now(started_at.tzinfo),
            type(error).__name__,
        )

    def close(self):
        if self._provider:
            self._provider.shutdown()
