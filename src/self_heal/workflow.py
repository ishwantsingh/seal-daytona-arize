"""Trusted, persisted descriptions of the runnable harness topology.

The workflow is deliberately derived from pinned source and trusted runtime
configuration.  It is not an LLM drawing and it does not execute candidate
code while extracting metadata.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from self_heal.contracts import canonical_hash, utc_now
from self_heal.settings import AnalystConfig


GRAPH_SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "trusted-static-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class WorkflowSnapshot:
    revision_id: str
    identity_hash: str
    graph_hash: str
    task_family: str
    source_commit: str
    graph: dict[str, Any]
    record: dict[str, Any]


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _relative(path: Path, source: Path) -> str:
    try:
        return str(path.relative_to(source))
    except ValueError:
        return path.name


def _node(
    node_id: str, kind: str, label: str, *, group: str | None = None,
    summary: str | None = None, source: Iterable[dict[str, str]] = (), layout: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    payload = {
        "id": node_id,
        "kind": kind,
        "label": label,
        "group": group,
        "summary": summary or "",
        "source": list(source),
        "layout": {"x": layout[0], "y": layout[1]},
    }
    payload["fingerprint"] = canonical_hash({key: value for key, value in payload.items() if key != "layout"})
    return payload


def _edge(source: str, target: str, relation: str, label: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}>{relation}>{target}", "source": source, "target": target,
            "relation": relation, "label": label or relation.replace("_", " ")}


def _source_refs(source: Path, paths: Iterable[str]) -> dict[str, list[dict[str, str]]]:
    refs: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        candidate = source / path
        digest = _digest(candidate)
        if digest:
            refs[path] = [{"path": _relative(candidate, source), "sha256": digest}]
    return refs


def _tool_names(source: Path, *, logistics: bool) -> list[str]:
    path = source / "harness" / ("logistics.py" if logistics else "tools.py")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    names = re.findall(r'(?:(?:"name"\s*:\s*)|(?:TOOL_NAME\s*=\s*))"([A-Za-z][A-Za-z0-9_]*)"', content)
    return list(dict.fromkeys(names))


def _inventory_graph(source: Path, config: AnalystConfig) -> dict[str, Any]:
    refs = _source_refs(source, ("harness/agent.py", "harness/tools.py", "harness/context.py"))
    tools = _tool_names(source, logistics=False) or ["inspect_table", "read_rows", "calculate"]
    nodes = [
        _node("context:task", "context_source", "Task input", summary="Validated question or structured task.", layout=(65, 105)),
        _node("context:policy", "context_source", "Harness policy", summary="Bounded instructions and output contract.", source=refs.get("harness/context.py", []), layout=(65, 225)),
        _node("context:history", "context_source", "Recent tool results", summary="Bounded prior tool exchanges.", source=refs.get("harness/context.py", []), layout=(65, 345)),
        _node("agent:inventory", "agent", "Inventory analyst", summary="Bounded model and tool loop.", source=refs.get("harness/agent.py", []), layout=(315, 225)),
        _node("group:inventory-tools", "tool_group", "Tools", group="agent:inventory", summary="Registered model-facing tools.", layout=(520, 78)),
        _node("adapter:table", "data_adapter", "Scoped table interface", summary="Run-bound reads with page and byte limits.", layout=(745, 210)),
        _node("data:sqlite", "data_source", "SQLite assigned dataset", summary="Immutable inventory rows selected for this run.", layout=(925, 210)),
        _node("external:model", "external_system", "Model provider", summary="Configured model API used through the trusted client.", layout=(525, 400)),
        _node("supervisor:telemetry", "supervisor", "Tracing adapter", summary="Trusted telemetry; not a model-facing tool.", layout=(745, 400)),
    ]
    tool_y = 145
    for index, name in enumerate(tools):
        nodes.append(_node(f"tool:{name}", "tool", name, group="agent:inventory",
                           summary="Registered inventory tool.", source=refs.get("harness/tools.py", []),
                           layout=(545, tool_y + index * 82)))
    edges = [
        _edge("context:task", "agent:inventory", "supplies_context"),
        _edge("context:policy", "agent:inventory", "supplies_context"),
        _edge("context:history", "agent:inventory", "supplies_context"),
        _edge("agent:inventory", "external:model", "calls_external"),
        _edge("agent:inventory", "supervisor:telemetry", "observed_by"),
        _edge("adapter:table", "data:sqlite", "reads_from"),
    ]
    for name in tools:
        edges.extend((_edge("agent:inventory", f"tool:{name}", "invokes"),
                      _edge(f"tool:{name}", "adapter:table", "reads_via")))
    return {"nodes": nodes, "edges": edges, "metadata": {
        "runtime_mcp_servers": [],
        "boundaries": ["editable harness", "trusted host"],
        "execution_mode": "model_tool_loop",
    }}


def _logistics_graph(source: Path, config: AnalystConfig) -> dict[str, Any]:
    refs = _source_refs(source, ("harness/logistics.py", "harness/agent.py"))
    tools = _tool_names(source, logistics=True)
    has_logistics_agent = (source / "harness" / "logistics.py").is_file()
    agent_label = "Logistics agent" if has_logistics_agent else "No logistics agent registered"
    agent_summary = ("Direct reviewed tool dispatch; it makes zero model calls."
                     if has_logistics_agent else
                     "This pinned harness has no logistics agent or registered logistics tool.")
    group_summary = ("Registered logistics tools." if has_logistics_agent else
                     "No tools are registered for the logistics task family.")
    nodes = [
        _node("context:task", "context_source", "Recognized logistics request", summary="The reviewed threshold request and bounded arguments.", source=refs.get("harness/agent.py", []), layout=(70, 170)),
        _node("agent:logistics", "agent", agent_label, summary=agent_summary, source=refs.get("harness/logistics.py", []), layout=(305, 205)),
        _node("group:logistics-tools", "tool_group", "Tools", group="agent:logistics", summary=group_summary, layout=(505, 100)),
        _node("supervisor:telemetry", "supervisor", "Tracing adapter", summary="Trusted telemetry; not a callable tool.", layout=(525, 400)),
    ]
    if has_logistics_agent:
        nodes.extend((
            _node("adapter:logistics", "data_adapter", "Scoped logistics session", summary="Bounded catalog and shipment reads with frozen time semantics.", layout=(735, 210)),
            _node("data:logistics", "data_source", "SQLite logistics bundle", summary="Assigned customers, warehouses, and shipments bundle.", layout=(925, 210)),
        ))
    for index, name in enumerate(tools):
        nodes.append(_node(f"tool:{name}", "tool", name, group="agent:logistics",
                           summary="Reviewed logistics tool.", source=refs.get("harness/logistics.py", []),
                           layout=(535, 168 + index * 90)))
    edges = [_edge("context:task", "agent:logistics", "supplies_context"),
             _edge("agent:logistics", "supervisor:telemetry", "observed_by")]
    if has_logistics_agent:
        edges.append(_edge("adapter:logistics", "data:logistics", "reads_from"))
    for name in tools:
        edges.extend((_edge("agent:logistics", f"tool:{name}", "invokes"),
                      _edge(f"tool:{name}", "adapter:logistics", "reads_via")))
    return {"nodes": nodes, "edges": edges, "metadata": {
        "runtime_mcp_servers": [],
        "boundaries": ["editable harness", "trusted host"],
        "execution_mode": "direct_tool_dispatch",
        "missing_components": [] if has_logistics_agent else ["logistics agent", "logistics tools", "logistics data adapter"],
    }}


def extract_workflow(*, source: Path, source_commit: str, config: AnalystConfig, task_family: str | None = None) -> WorkflowSnapshot:
    """Derive one immutable workflow snapshot from a clean, pinned source tree."""

    family = task_family or config.task_family
    source = source.resolve()
    graph = _logistics_graph(source, config) if family == "logistics-shipment-threshold" else _inventory_graph(source, config)
    graph["nodes"].sort(key=lambda node: node["id"])
    graph["edges"].sort(key=lambda edge: edge["id"])
    graph_hash = canonical_hash(graph)
    identity = {
        "task_family": family,
        "source_commit": source_commit,
        "config_hash": config.config_hash,
        "contract_hash": config.contract_hash,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
    }
    identity_hash = canonical_hash(identity)
    revision_id = "workflow_" + identity_hash[:32]
    record = {
        "_id": revision_id,
        "workflow_revision_id": revision_id,
        "task_family": family,
        "source_commit": source_commit,
        "configuration_hash": config.config_hash,
        "contract_hash": config.contract_hash,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "identity_hash": identity_hash,
        "graph_hash": graph_hash,
        "graph": graph,
        "created_at": utc_now(),
    }
    return WorkflowSnapshot(revision_id, identity_hash, graph_hash, family, source_commit, graph, record)


def proposal_overlay(*, base_revision_id: str, changed_mechanism: str, hypothesis: str, diff: str | None) -> dict[str, Any]:
    """Describe an untrusted proposal separately from executable graph metadata."""

    additions = []
    for match in re.finditer(r"^\+.*?(?:TOOL_NAME\s*=\s*|\"name\"\s*:\s*)\"([A-Za-z][A-Za-z0-9_]*)\"", diff or "", re.MULTILINE):
        name = match.group(1)
        additions.append({"id": f"proposed-tool:{name}", "kind": "tool", "label": name,
                          "group": None, "status": "proposed"})
    return {
        "base_workflow_revision_id": base_revision_id,
        "changed_mechanism": changed_mechanism,
        "hypothesis": hypothesis,
        "additions": additions,
        "diff_hash": canonical_hash(diff or ""),
    }


def workflow_diff(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a structural diff using stable node and edge identities."""

    base_graph, candidate_graph = base["graph"], candidate["graph"]
    before_nodes = {node["id"]: node for node in base_graph.get("nodes", [])}
    after_nodes = {node["id"]: node for node in candidate_graph.get("nodes", [])}
    before_edges = {edge["id"]: edge for edge in base_graph.get("edges", [])}
    after_edges = {edge["id"]: edge for edge in candidate_graph.get("edges", [])}
    return {
        "base_workflow_revision_id": base["workflow_revision_id"],
        "workflow_revision_id": candidate["workflow_revision_id"],
        "nodes": {
            "added": [after_nodes[key] for key in sorted(after_nodes.keys() - before_nodes.keys())],
            "removed": [before_nodes[key] for key in sorted(before_nodes.keys() - after_nodes.keys())],
            "changed": [{"before": before_nodes[key], "after": after_nodes[key]} for key in sorted(before_nodes.keys() & after_nodes.keys()) if before_nodes[key].get("fingerprint") != after_nodes[key].get("fingerprint")],
        },
        "edges": {
            "added": [after_edges[key] for key in sorted(after_edges.keys() - before_edges.keys())],
            "removed": [before_edges[key] for key in sorted(before_edges.keys() - after_edges.keys())],
        },
    }
