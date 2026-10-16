"""Credential-free SEAL console for recording the end-to-end demo video.

Everything below the model and cloud boundaries is the real product: the web
API and UI, SQLite history, the editable harness, the evolution controller,
patch screening, Git worktrees, paired selection, the protected oracle, and
promotion. Three external services are replaced with local stand-ins so the
demo runs without accounts:

* OpenRouter -> ``PolicyModel`` (a deterministic tool-using agent) and
  ``ReplayProposer`` (returns the repair recorded in docs/verified-repair.patch,
  which a real proposer generated during the live run).
* Daytona -> the test-only local process transport (same container entry point
  and bridge protocol, no cloud isolation).
* Arize Phoenix -> an in-memory OpenTelemetry exporter with a local span store.

Run from the repository root (commit first: evolution refuses dirty sources):

    uv run python scripts/demo/demo_server.py --port 4173
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from evals.analyst.generator import load_scenarios, prepare_case  # noqa: E402
from runner_transport import LocalProcessTransport  # noqa: E402
from self_heal import web  # noqa: E402
from self_heal.controller import EvolutionController  # noqa: E402
from self_heal.database import SQLiteDatabase  # noqa: E402
from self_heal.model import ModelReply, ToolCall  # noqa: E402
from self_heal.repository import CandidateRepository  # noqa: E402
from self_heal.runner import CandidateRunner  # noqa: E402
from self_heal.settings import PhoenixConfig, load_config  # noqa: E402
from self_heal.storage import HistoryStore  # noqa: E402
from self_heal.table_store import TableStore  # noqa: E402
from self_heal.telemetry import PhoenixTelemetry  # noqa: E402

DATASET_ID = "warehouse-inventory"
METRICS = {"available": "available", "on-hand": "on_hand", "on hand": "on_hand", "reserved": "reserved"}


class LocalPhoenix:
    """Serves finished in-memory spans through the Phoenix client calls SEAL uses."""

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self.exporter = exporter
        self.spans = self
        self.projects = SimpleNamespace(get=lambda **kw: {"id": "local-project"})

    def get_spans(self, *, trace_ids, **kwargs):
        results = []
        for span in self.exporter.get_finished_spans():
            trace_id = format(span.context.trace_id, "032x")
            if trace_id not in trace_ids:
                continue
            results.append({
                "name": span.name,
                "context": {"trace_id": trace_id, "span_id": format(span.context.span_id, "016x")},
                "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
                "attributes": dict(span.attributes),
                "span_kind": span.attributes.get("openinference.span.kind"),
                "status_code": span.status.status_code.name,
                "start_time": datetime.fromtimestamp(span.start_time / 1e9, timezone.utc).isoformat(),
                "end_time": datetime.fromtimestamp(span.end_time / 1e9, timezone.utc).isoformat(),
            })
        return results

    def add_span_annotation(self, **kwargs):
        return {"id": "annotation"}


def interpret(question: str) -> dict[str, Any] | None:
    """Map an inventory question to the task contract the way the agent model does."""

    text = question.lower()
    metric = next((value for key, value in METRICS.items() if key in text), None)
    if metric is None or "revenue" in text:
        return None
    task: dict[str, Any] = {"metric": metric}
    group = re.search(r"(?:each|every|per|by)\s+(warehouse|category)", text)
    if group:
        task["group_by"] = group.group(1)
    sku = re.search(r"sku[- ]?(\d+)", text)
    place = re.search(r"in the (\w+) (warehouse|category)", question, flags=re.IGNORECASE)
    named = re.search(r"for (warehouse|category|sku) (\S+?)\??$", question, flags=re.IGNORECASE)
    if named:
        task.update(filter_field=named.group(1).lower(), filter_value=named.group(2))
    elif sku:
        task.update(filter_field="sku", filter_value=f"SKU-{int(sku.group(1)):05d}")
    elif place:
        task.update(filter_field=place.group(2).lower(), filter_value=place.group(1).capitalize())
    return task


class PolicyModel:
    """Deterministic stand-in for the OpenRouter agent model.

    It uses whatever tools the harness exposes: a bulk aggregation tool when one
    exists, otherwise bounded read_rows paging, which runs out of model calls on
    large tables exactly like the recorded live baseline.
    """

    model = "local-policy-agent"
    tracing_settings = {"temperature": 0, "tool_choice": "auto", "parallel_tool_calls": False}

    def __init__(self, latency: float = 0.0) -> None:
        self.latency = latency
        self.seen: set[str] = set()
        self.totals: dict[str, int] = {}
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        time.sleep(self.latency)
        if not tools:
            task = interpret(messages[-1]["content"])
            return ModelReply(json.dumps(task or {"error": "Unsupported request"}), (), 60)
        task = json.loads(re.search(r"(?:Validated task|structured task): (\{.*\})", messages[1]["content"]).group(1))
        names = {tool["function"]["name"] for tool in tools}
        last = messages[-1]
        if last["role"] == "tool" and last["tool_call_id"] not in self.seen:
            self.seen.add(last["tool_call_id"])
            result = json.loads(last["content"])
            if "value" in result or "groups" in result:
                return ModelReply(json.dumps({k: result[k] for k in ("value", "groups") if k in result}), (), 90)
            for row in result.get("rows", []):
                value = row["on_hand"] - row["reserved"] if task["metric"] == "available" else row[task["metric"]]
                key = row[task["group_by"]] if task.get("group_by") else "value"
                self.totals[key] = self.totals.get(key, 0) + value
            if result.get("next_cursor") is None:
                if task.get("group_by"):
                    return ModelReply(json.dumps({"groups": dict(sorted(self.totals.items()))}), (), 90)
                return ModelReply(json.dumps({"value": self.totals.get("value", 0)}), (), 90)
            cursor = result["next_cursor"]
        else:
            cursor = None
        self.calls += 1
        if "aggregate_metric" in names:
            arguments = {key: task[key] for key in ("metric", "group_by", "filter_field", "filter_value") if key in task}
            return ModelReply(None, (ToolCall(f"call-{self.calls}", "aggregate_metric", json.dumps(arguments)),), 120)
        arguments = {"limit": 4}
        if cursor:
            arguments["cursor"] = cursor
        if task.get("filter_field"):
            arguments.update(filter_field=task["filter_field"], filter_value=task["filter_value"])
        return ModelReply(None, (ToolCall(f"call-{self.calls}", "read_rows", json.dumps(arguments)),), 120)


class ReplayProposer:
    """Stand-in for the OpenRouter evolution model.

    The scenario reply describes a fresh reproduction of the observed task. The
    patch reply is the repair a real proposer generated in the live Daytona and
    Phoenix run (docs/verified-repair.patch), returned as whole files.
    """

    model = "recorded-proposer"

    def __init__(self, files: dict[str, str], latency: float) -> None:
        self.files, self.latency = files, latency

    def complete(self, messages, tools):
        return self.complete_json(messages)

    def complete_json(self, messages, schema=None):
        time.sleep(self.latency)
        payload = json.loads(messages[-1]["content"])
        if "editable_source" not in payload:
            question = payload["incident"]["question"]
            task = interpret(question)
            return ModelReply(json.dumps({
                "description": "A fresh 384-row inventory table that needs a whole-table grouped total.",
                "task": task,
                "question": question,
                "generation": {"seed": 4242, "row_count": 384, "warehouses": ["East", "North", "South", "West"],
                               "categories": ["Hardware", "Office", "Safety"], "on_hand": [1, 100]},
                "requested_capability": "Aggregate a whole table without one model call per page",
            }), (), 900)
        return ModelReply(json.dumps({
            "hypothesis": ("The agent pages through the table four rows per model call, so any table over "
                           "about 28 rows exhausts the 8-call budget before it can answer."),
            "changed_mechanism": "Add a bounded aggregate_metric tool that scans every page inside one tool call",
            "files": [{"path": path, "content": content} for path, content in self.files.items()],
        }), (), 4200)


def recorded_repair() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        shutil.copytree(ROOT / "harness", target / "harness")
        subprocess.run(["git", "apply", str(ROOT / "docs" / "verified-repair.patch")], cwd=target, check=True)
        return {f"harness/{name}": (target / "harness" / name).read_text() for name in ("context.py", "tools.py")}


class LocalCandidateRunner(CandidateRunner):
    """The real candidate runner, with the sandbox swapped for a local process."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("transport_factory", LocalProcessTransport)
        super().__init__(*args, **kwargs)

    def image_identity(self):
        return "local-process:demo"


def build_application(database_path: Path, *, agent_latency: float, proposer_latency: float):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)
    config = load_config(ROOT / "config" / "analyst.yaml")
    database = SQLiteDatabase(database_path)
    store = TableStore(database, config)
    store.ensure_indexes()
    history = HistoryStore(database)
    history.ensure_indexes()
    scenario = load_scenarios(ROOT / "evals" / "analyst" / "scenarios.yaml", config)["bulk-warehouse-available"]
    store.materialize(DATASET_ID, list(prepare_case(scenario, config).rows))

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = PhoenixConfig(enabled=True, api_key=None, project="seal-demo", base_url="http://localhost:6006")
    telemetry = PhoenixTelemetry(tracing, provider=provider, client=LocalPhoenix(exporter),
                                 verification_attempts=1, retry_delay_seconds=0)
    files = recorded_repair()
    lock = Lock()

    def controller_factory(on_progress):
        with lock:
            return EvolutionController(
                store=store, history=history, config=config, telemetry=telemetry,
                repository=CandidateRepository(ROOT),
                runner=LocalCandidateRunner(store=store, config=config, history=history, telemetry=telemetry),
                model_factory=PolicyModel, evolution_model=ReplayProposer(files, proposer_latency),
                on_progress=on_progress,
            )

    web.CandidateRunner = LocalCandidateRunner
    return web.WebApplication(
        store=store, history=history, config=config, telemetry=telemetry,
        model_factory=lambda: PolicyModel(agent_latency), tracing=tracing,
        evolution_controller_factory=controller_factory,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--database", type=Path, default=ROOT / ".seal" / "demo.sqlite3")
    parser.add_argument("--agent-latency", type=float, default=0.5)
    parser.add_argument("--proposer-latency", type=float, default=1.5)
    args = parser.parse_args()
    application = build_application(args.database, agent_latency=args.agent_latency,
                                     proposer_latency=args.proposer_latency)
    server = web.create_server(application, port=args.port)
    print(f"SEAL demo console at http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
