"""Phase 1 commands for seeding SQLite and running the analyst."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from self_heal.database import SQLiteDatabase, DatabaseError

from evals.analyst.generator import (
    ScenarioError,
    load_scenarios,
    materialize_case,
    prepare_case,
)
from evals.analyst.oracle import OracleError
from harness.agent import RunResult
from harness.tools import AnalystTools
from self_heal.contracts import (
    build_eval_case_record,
    stable_case_id,
    utc_now,
    canonical_hash,
    model_identity,
)
from self_heal.evaluation import EvaluationRunner, baseline_expectation_matches
from self_heal.execution import RunExecution, RunExecutor
from self_heal.model import OpenRouterModel
from self_heal.settings import (
    AnalystConfig,
    agent_model_config,
    database_path,
    phoenix_config,
    load_config,
)
from self_heal.storage import HistoryStore, HistoryError
from self_heal.table_store import TableStore, DatasetError
from self_heal.telemetry import PhoenixTelemetry
from self_heal.phoenix_evaluations import PhoenixEvaluationPublisher
from self_heal.controller import EvolutionController, EvolutionBlocked
from self_heal.repository import CandidateRepository, PatchRejected
from self_heal.runner import CandidateRunner, RunnerError
from self_heal.promotion import PromotionManager, PromotionRejected
from self_heal.settings import evolution_model_config
from self_heal.final_assessment import FinalAssessmentError, reserve_cases, assess_cases
from self_heal.logistics_store import LogisticsDatasetStore
from self_heal.logistics_evaluation import run_logistics_checks
from self_heal.logistics_evolution import (
    FamilyEvolutionController,
    LogisticsEvolutionController,
)
from evals.logistics.generator import public_incident_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seal", description="SQLite-backed structured table analyst"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "doctor", help="Check local configuration and service connectivity"
    )
    subcommands.add_parser(
        "demo", help="Run the public failure, repair, selection, and verification flow"
    )
    subcommands.add_parser(
        "phoenix-sync",
        help="Retry incomplete evaluation exports without rerunning agents",
    )
    seed = subcommands.add_parser(
        "seed", help="Materialize an immutable table in SQLite"
    )
    seed.add_argument(
        "--fixture", required=True, type=Path, help="JSON dataset definition"
    )
    subcommands.add_parser(
        "seed-logistics",
        help="Materialize the public customers/warehouses/shipments bundle",
    )
    run = subcommands.add_parser(
        "run", help="Run an analyst task against an SQLite dataset"
    )
    task_input = run.add_mutually_exclusive_group(required=True)
    task_input.add_argument("--task", help="JSON task object for reproducible runs")
    task_input.add_argument(
        "--question", help="Natural-language question about the assigned table"
    )
    run.add_argument("--dataset", required=True, help="Dataset ID to bind to this run")
    run.add_argument(
        "--json",
        action="store_true",
        help="Show the full result as JSON, including for questions",
    )
    evaluation = subcommands.add_parser(
        "eval", help="Materialize or run protected Phase 2 scenarios"
    )
    evaluation_commands = evaluation.add_subparsers(
        dest="evaluation_command", required=True
    )
    materialize = evaluation_commands.add_parser(
        "materialize", help="Publish declared valid evaluation datasets to SQLite"
    )
    materialize.add_argument("--scenario", default="all", help="Scenario ID or all")
    evaluate = evaluation_commands.add_parser(
        "run", help="Run the current harness against one declared scenario"
    )
    evaluate.add_argument("--scenario", required=True, help="Answerable scenario ID")
    evaluation_commands.add_parser(
        "logistics",
        help="Check the reviewed logistics tool against the protected oracle",
    )
    history = subcommands.add_parser(
        "history", help="Read compact supervisor evidence from SQLite"
    )
    history_commands = history.add_subparsers(dest="history_command", required=True)
    gaps = history_commands.add_parser(
        "capability-gaps", help="List explicit unsupported requests"
    )
    gaps.add_argument("--task-family", help="Limit results to one task family")
    gaps.add_argument(
        "--limit", type=int, default=20, help="Maximum records to return (1-100)"
    )
    recorded_run = history_commands.add_parser(
        "run", help="Read one compact run record"
    )
    recorded_run.add_argument("--run-id", required=True)
    attempts = history_commands.add_parser(
        "candidates", help="Find prior candidate attempts"
    )
    attempts.add_argument("--task-family", required=True)
    attempts.add_argument("--changed-mechanism")
    attempts.add_argument(
        "--limit", type=int, default=20, help="Maximum records to return (1-100)"
    )
    history_commands.add_parser("active", help="Show the active pinned harness version")
    lineage = history_commands.add_parser(
        "lineage",
        help="Show an incident, cases, candidates, trials, and final outcomes",
    )
    lineage.add_argument("--run-id", required=True)
    history_commands.add_parser(
        "final", help="Show final assessment attempts separately from selection"
    )
    final = subcommands.add_parser(
        "final", help="Reserve and run one-use untouched assessment cases"
    )
    final_commands = final.add_subparsers(dest="final_command", required=True)
    final_reserve = final_commands.add_parser(
        "reserve", help="Publish fresh final datasets and a protected manifest"
    )
    final_reserve.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="New manifest path outside the checkout",
    )
    final_assess = final_commands.add_parser(
        "assess", help="Run active commit once on reserved cases"
    )
    final_assess.add_argument(
        "--manifest", required=True, type=Path, help="Protected manifest path"
    )
    evolve = subcommands.add_parser(
        "evolve", help="Diagnose and evolve one recorded limitation"
    )
    evolve.add_argument("--run-id", required=True, help="Completed SQLite run ID")
    evolve.add_argument(
        "--rescreen-candidate", help="Retry one stored patch after a screening fix"
    )
    runner = subcommands.add_parser(
        "runner", help="Manage the isolated candidate image"
    )
    runner_commands = runner.add_subparsers(dest="runner_command", required=True)
    runner_commands.add_parser("build", help="Build the Daytona runner snapshot")
    rollback = subcommands.add_parser(
        "rollback", help="Activate a retained previous version"
    )
    rollback.add_argument("--expected-active", required=True)
    rollback.add_argument("--commit", required=True)
    rollback.add_argument("--reason", required=True)
    ui = subcommands.add_parser("ui", help="Start the local operator interface")
    ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="Local address to listen on (default: 127.0.0.1)",
    )
    ui.add_argument(
        "--port", type=int, default=4173, help="Local port to listen on (default: 4173)"
    )
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
        flush=True,
    )


def _answer_message(task: dict[str, Any], answer: dict[str, Any]) -> str:
    if "metric" not in task:
        if "value" in answer:
            return f"The result is {answer['value']}."
        return "; ".join(f"{name}: {value}" for name, value in answer["groups"].items()) or "No matching groups."
    metric = {"available": "available", "on_hand": "on-hand", "reserved": "reserved"}[
        task["metric"]
    ]
    field, filter_value = task.get("filter_field"), task.get("filter_value")
    location = {
        "warehouse": f" in the {filter_value} warehouse",
        "category": f" in the {filter_value} category",
        "sku": f" for SKU {filter_value}",
    }.get(field, "")
    if "groups" in answer:
        groups = answer["groups"]
        if not groups:
            return "I found no matching rows to group."
        values = "; ".join(f"{name}: {value}" for name, value in groups.items())
        return f"{metric.capitalize()} units by {task['group_by'].replace('_', ' ')}{location}: {values}."
    value = answer["value"]
    unit = "unit" if value == 1 else "units"
    return f"There are {value} {metric} {unit}{location or ' in this dataset'}."


def _present_run(
    result: RunResult,
    *,
    conversational: bool,
    json_output: bool,
    execution: RunExecution | None = None,
) -> int:
    if result.outcome == "unsupported":
        message = (result.limitation_reason if result.limitation_kind == "clarification_required"
                   else "I can't answer that with my current capabilities.")
    elif result.outcome == "error":
        message = f"Sorry, I couldn't complete that request: {result.error}."
    else:
        assert result.answer is not None and result.interpreted_task is not None
        message = _answer_message(result.interpreted_task, result.answer)

    if conversational and not json_output:
        print(message)
    else:
        evidence = (
            execution.compact_evidence()
            if execution is not None
            else {
                "trace": {"id": None, "url": None, "status": "not_recorded"},
                "history": {"status": "not_recorded", "error_type": None},
            }
        )
        _emit(
            {
                "run_id": result.run_id,
                "answer": result.answer,
                "error": result.error,
                "outcome": result.outcome,
                "message": message,
                "interpreted_task": result.interpreted_task,
                "model_calls": result.model_calls,
                "tool_calls": result.tool_calls,
                "total_tokens": result.total_tokens,
                "elapsed_seconds": result.elapsed_seconds,
                "table_pages": result.table_pages,
                "table_bytes": result.table_bytes,
                "limitation_kind": result.limitation_kind,
                "limitation_reason": result.limitation_reason,
                "capability_request": result.capability_request,
                **evidence,
            }
        )
    return 1 if result.outcome == "error" else 0


def _select_scenarios(
    scenarios: dict[str, Any], scenario_id: str, *, allow_all: bool
) -> list[Any]:
    if scenario_id == "all" and allow_all:
        return list(scenarios.values())
    try:
        return [scenarios[scenario_id]]
    except KeyError as exc:
        raise ValueError("Unknown evaluation scenario") from exc


def _run_evaluation_command(
    args: argparse.Namespace,
    store: TableStore,
    config: AnalystConfig,
    history: HistoryStore,
) -> int:
    scenarios = load_scenarios(config.evaluation.scenarios_path, config)
    if args.evaluation_command == "materialize":
        selected = _select_scenarios(scenarios, args.scenario, allow_all=True)
        published = []
        history_status = "recorded"
        for scenario in selected:
            if scenario.invalid_row is not None:
                continue
            prepared = prepare_case(scenario, config)
            materialized = materialize_case(store, prepared)
            try:
                case_id = stable_case_id(
                    scenario_id=scenario.scenario_id,
                    dataset=materialized.dataset,
                    task=prepared.scenario.task,
                    oracle_version=config.evaluation.oracle_version,
                )
                history.record_eval_case(
                    build_eval_case_record(
                        case_id=case_id,
                        scenario_id=scenario.scenario_id,
                        dataset=materialized.dataset,
                        task=prepared.scenario.task,
                        expected_answer=prepared.expected_answer,
                        oracle_version=config.evaluation.oracle_version,
                        config=config,
                        exposure_role="baseline",
                        created_at=utc_now(),
                    )
                )
            except HistoryError:
                history_status = "incomplete"
            published.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "dataset_id": materialized.dataset.dataset_id,
                    "seed": scenario.seed,
                    "row_count": materialized.dataset.row_count,
                    "content_hash": materialized.dataset.content_hash,
                }
            )
        _emit(
            {
                "materialized": published,
                "skipped_invalid_scenarios": [
                    s.scenario_id for s in selected if s.invalid_row
                ],
                "history": {"status": history_status},
            }
        )
        return 0

    scenario = _select_scenarios(scenarios, args.scenario, allow_all=False)[0]
    case = prepare_case(scenario, config)
    api_key, model_id = agent_model_config()
    executor = RunExecutor(
        history=history,
        telemetry=PhoenixTelemetry(phoenix_config()),
        config=config,
    )
    trial = EvaluationRunner(
        store, config, executor=executor, history=history
    ).run_case(case, OpenRouterModel(api_key, model_id))
    payload = trial.to_dict()
    payload["baseline_expectation"] = scenario.baseline_expectation
    payload["baseline_expectation_matched"] = baseline_expectation_matches(
        trial, scenario.baseline_expectation
    )
    _emit(payload)
    return 0 if payload["baseline_expectation_matched"] else 1


def _history_run_summary(record: dict[str, Any]) -> dict[str, Any]:
    invocation = record.get("invocation") or {}
    return {
        "run_id": record.get("run_id"),
        "created_at": record.get("created_at"),
        "outcome": record.get("outcome"),
        "limitation_kind": record.get("limitation_kind"),
        "limitation_reason": record.get("limitation_reason"),
        "question": invocation.get("question"),
        "task_family": invocation.get("task_family"),
        "dataset": record.get("dataset"),
        "resources": record.get("resources"),
        "trace": record.get("trace"),
        "history_status": record.get("status"),
    }


def _run_history_command(args: argparse.Namespace, history: HistoryStore) -> int:
    if args.history_command == "active":
        _emit({"active": history.active_version(load_config().task_family)})
        return 0
    if args.history_command == "capability-gaps":
        records = history.capability_gaps(
            task_family=args.task_family, limit=args.limit
        )
        _emit({"runs": [_history_run_summary(record) for record in records]})
        return 0
    if args.history_command == "run":
        record = history.get_run(args.run_id)
        if record is None:
            raise ValueError("Run history is unavailable")
        _emit(_history_run_summary(record))
        return 0
    if args.history_command == "final":
        records = history.final_assessments.find({}).sort("started_at", -1).limit(50)
        _emit(
            {
                "final_assessments": [
                    {key: value for key, value in record.items() if key != "_id"}
                    for record in records
                ]
            }
        )
        return 0
    if args.history_command == "lineage":
        incident = history.get_run(args.run_id)
        if incident is None:
            raise ValueError("Run history is unavailable")
        candidates = list(
            history.candidates.find({"incident_run_id": args.run_id}).sort(
                "created_at", 1
            )
        )
        cases = list(
            history.eval_cases.find(
                {
                    "scenario_id": {
                        "$regex": "^observed-" + args.run_id.replace("-", "")[:24]
                    }
                }
            )
        )
        candidate_summaries = []
        for candidate in candidates:
            selection = candidate.get("selection") or {}
            candidate_summaries.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "status": candidate.get("status"),
                    "hypothesis": candidate.get("hypothesis"),
                    "changed_mechanism": candidate.get("changed_mechanism"),
                    "parent_commit": candidate.get("parent_commit"),
                    "candidate_commit": candidate.get("candidate_commit"),
                    "diff": candidate.get("diff"),
                    "accepted": selection.get("accepted"),
                    "reasons": selection.get("reasons"),
                    "trials": selection.get("trials", []),
                }
            )
        active = history.active_version(load_config().task_family)
        final_records = (
            list(
                history.final_assessments.find({"commit": active.get("commit")}).sort(
                    "started_at", 1
                )
            )
            if active
            else []
        )
        _emit(
            {
                "incident": _history_run_summary(incident),
                "gap": history.get_gap(args.run_id),
                "cases": [
                    {
                        "case_id": item.get("case_id"),
                        "scenario_id": item.get("scenario_id"),
                        "dataset": item.get("dataset"),
                        "exposures": item.get("exposures"),
                    }
                    for item in cases
                ],
                "candidates": candidate_summaries,
                "active_commit": active.get("commit") if active else None,
                "final_assessments": [
                    {key: value for key, value in record.items() if key != "_id"}
                    for record in final_records
                ],
            }
        )
        return 0
    records = history.candidates_for(
        task_family=args.task_family,
        changed_mechanism=args.changed_mechanism,
        limit=args.limit,
    )
    _emit(
        {
            "candidates": [
                {
                    "candidate_id": record.get("candidate_id"),
                    "candidate_commit": record.get("candidate_commit"),
                    "parent_commit": record.get("parent_commit"),
                    "task_family": record.get("task_family"),
                    "changed_mechanism": record.get("changed_mechanism"),
                    "status": record.get("status"),
                    "created_at": record.get("created_at"),
                }
                for record in records
            ]
        }
    )
    return 0


def _run_ui_command(
    args: argparse.Namespace,
    store: TableStore,
    config: AnalystConfig,
    history: HistoryStore,
    logistics: LogisticsDatasetStore,
) -> int:
    """Start the browser surface on loopback by default.

    The UI is an alternate operator interface, not a second execution path:
    its requests instantiate the same model, runner, telemetry, and SQLite
    history components as the existing ``run`` command.
    """

    from self_heal.web import WebApplication, create_server

    api_key, model_id = agent_model_config()
    tracing = phoenix_config()
    try:
        evolution_key, evolution_id = evolution_model_config()
    except ValueError:
        evolution_key = evolution_id = None

    def evolution_controller_factory(on_progress):
        if not evolution_key or not evolution_id:
            raise RuntimeError("OPENROUTER_EVOLUTION_MODEL is not configured")
        job_telemetry = PhoenixTelemetry(tracing)
        repository = CandidateRepository(Path.cwd())
        agent_model = lambda: OpenRouterModel(api_key, model_id)
        evolution_model = lambda: OpenRouterModel(
            evolution_key, evolution_id, timeout_seconds=120
        )

        def inventory_controller():
            return EvolutionController(
                store=store,
                history=history,
                config=config,
                telemetry=job_telemetry,
                repository=repository,
                runner=CandidateRunner(
                    store=store,
                    logistics=logistics,
                    config=config,
                    history=history,
                    telemetry=job_telemetry,
                    image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
                ),
                model_factory=agent_model,
                evolution_model=evolution_model(),
                on_progress=on_progress,
            )

        def logistics_controller():
            contract = (config.task_contracts or {}).get(
                "logistics-shipment-threshold-v1"
            )
            if not contract:
                raise RuntimeError("Logistics task contract is unavailable")
            scoped = replace(
                config,
                task_family="logistics-shipment-threshold",
                task_contract_version="logistics-shipment-threshold-v1",
                contract_hash=canonical_hash(contract),
            )
            return LogisticsEvolutionController(
                logistics=logistics,
                history=history,
                config=scoped,
                telemetry=job_telemetry,
                repository=repository,
                runner=CandidateRunner(
                    store=store,
                    logistics=logistics,
                    config=scoped,
                    history=history,
                    telemetry=job_telemetry,
                    image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
                ),
                model_factory=agent_model,
                evolution_model=evolution_model(),
                on_progress=on_progress,
            )

        return FamilyEvolutionController(
            history=history,
            inventory=inventory_controller,
            logistics=logistics_controller,
        )

    application = WebApplication(
        store=store,
        history=history,
        config=config,
        telemetry=PhoenixTelemetry(tracing),
        model_factory=lambda: OpenRouterModel(api_key, model_id),
        tracing=tracing,
        logistics=logistics,
        evolution_controller_factory=evolution_controller_factory
        if evolution_key
        else None,
    )
    server = create_server(application, host=args.host, port=args.port)
    address, port = server.server_address[:2]
    print(f"Self-Heal UI is running at http://{address}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSelf-Heal UI stopped.")
    finally:
        server.server_close()
    return 0


def _doctor(config: AnalystConfig) -> int:
    from phoenix.client import Client
    from urllib.parse import urlsplit

    checks = {}
    for key in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_AGENT_MODEL",
        "OPENROUTER_EVOLUTION_MODEL",
        "DAYTONA_API_KEY",
    ):
        checks[key] = {"status": "configured" if os.environ.get(key) else "missing"}
    try:
        with SQLiteDatabase(database_path()) as database:
            database.connection.execute("SELECT 1").fetchone()
        checks["sqlite"] = {"status": "ready", "path": str(database_path())}
    except Exception as exc:
        checks["sqlite"] = {"status": "error", "error_type": type(exc).__name__}
    tracing = phoenix_config()
    parsed = urlsplit(tracing.base_url)
    if parsed.hostname == "app.phoenix.arize.com" and not parsed.path.startswith("/s/"):
        checks["phoenix"] = {
            "status": "error",
            "reason": "Cloud URLs must include /s/YOUR_APP_HANDLE",
        }
    elif not tracing.enabled:
        checks["phoenix"] = {
            "status": "error",
            "reason": "PHOENIX_TRACING must be true for evolution",
        }
    else:
        try:
            Client(base_url=tracing.base_url, api_key=tracing.api_key).projects.list()
            checks["phoenix"] = {"status": "ready", "project": tracing.project}
        except Exception as exc:
            checks["phoenix"] = {"status": "error", "error_type": type(exc).__name__}
    try:
        runner = CandidateRunner(
            store=None,
            config=config,
            history=None,
            telemetry=PhoenixTelemetry(tracing),
            image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
        )
        checks["daytona"] = {
            "status": "ready",
            "runner_identity": runner.image_identity(),
        }
    except Exception as exc:
        checks["daytona"] = {
            "status": "error",
            "error_type": type(exc).__name__,
            "hint": "Check DAYTONA_API_KEY and run seal runner build",
        }
    import subprocess

    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5
    )
    checks["source"] = {
        "status": "ready"
        if status.returncode == 0 and not status.stdout.strip()
        else "dirty_or_uninitialized"
    }
    ready = all(item["status"] in {"ready", "configured"} for item in checks.values())
    _emit({"ready": ready, "checks": checks})
    return 0 if ready else 1


def _run_demo(store, history, logistics, config):
    agent_key, agent_id = agent_model_config()
    evolution_key, evolution_id = evolution_model_config()
    tracing = phoenix_config()
    telemetry = PhoenixTelemetry(tracing)
    model_factory = lambda: OpenRouterModel(agent_key, agent_id)
    scenario = load_scenarios(config.evaluation.scenarios_path, config)[
        "bulk-warehouse-available"
    ]
    case = prepare_case(scenario, config)
    dataset = materialize_case(store, case).dataset
    active = history.active_version(config.task_family)
    if active:
        _emit(
            {
                "stage": "already_active",
                "commit": active["commit"],
                "message": "Use a fresh SEAL_DATABASE_PATH for a new repair demonstration.",
            }
        )
        return 0
    _emit({"stage": "baseline", "scenario": scenario.scenario_id})
    trial = EvaluationRunner(
        store,
        config,
        executor=RunExecutor(history=history, telemetry=telemetry, config=config),
        history=history,
    ).run_case(case, model_factory())
    _emit({"stage": "incident", **trial.to_dict()})
    if trial.passed:
        _emit(
            {
                "status": "no_failure",
                "message": "The current harness passed; no repair is justified.",
            }
        )
        return 0
    if trial.trace_status != "available" or trial.history_status != "recorded":
        raise EvolutionBlocked(
            "The demo needs complete Phoenix trace and evaluation evidence"
        )
    repository = CandidateRepository(Path.cwd())
    runner = CandidateRunner(
        store=store,
        logistics=logistics,
        config=config,
        history=history,
        telemetry=telemetry,
        image=os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1"),
    )
    controller = EvolutionController(
        store=store,
        history=history,
        config=config,
        telemetry=telemetry,
        repository=repository,
        runner=runner,
        model_factory=model_factory,
        evolution_model=OpenRouterModel(
            evolution_key, evolution_id, timeout_seconds=120
        ),
        on_progress=lambda stage, payload: _emit(
            {"stage": stage, **{k: v for k, v in payload.items() if k != "diff"}}
        ),
    )
    result = controller.evolve(trial.run_id)
    _emit(
        {
            "stage": "decision",
            **{k: v for k, v in result.items() if k != "selection"},
            "selection": {
                k: v for k, v in result.get("selection", {}).items() if k != "trials"
            },
        }
    )
    if result["status"] != "activated":
        return 1
    commit = result["promotion"]["active_commit"]
    verification = runner.run(
        source=repository.active_checkout(commit),
        source_commit=commit,
        dataset=dataset,
        invocation=scenario.question or scenario.task,
        model=model_factory(),
    )
    passed = (
        verification.result.outcome == "answered"
        and verification.result.answer == case.expected_answer
    )
    from self_heal.contracts import build_evaluation_record, new_trial_id

    record = build_evaluation_record(
        evaluation_id=new_trial_id(),
        case_id=trial.case_id,
        result=verification.result,
        passed=passed,
        violation=None if passed else "wrong_or_missing_answer",
        dataset=dataset,
        trace=verification.trace,
        config=config,
        created_at=utc_now(),
    )
    record["role"] = "post_activation_verification"
    history.record_evaluation(record)
    _emit(
        {
            "stage": "verification",
            "passed": passed,
            "answer": verification.result.answer,
            "run_id": verification.result.run_id,
            "commit": commit,
            **verification.compact_evidence(),
        }
    )
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env")
    args = _parser().parse_args(argv)
    try:
        config = load_config()
        image = os.environ.get("DAYTONA_SNAPSHOT", "seal-python311-v1")
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "runner":
            builder = CandidateRunner(
                store=None,
                config=config,
                history=None,
                telemetry=PhoenixTelemetry(phoenix_config()),
                image=image,
            )
            _emit({"image": image, "digest": builder.build_image()})
            return 0
        with SQLiteDatabase(database_path()) as database:
            store = TableStore(database, config)
            store.ensure_indexes()
            history = HistoryStore(
                database,
                evaluation_sink=PhoenixEvaluationPublisher(
                    database, PhoenixTelemetry(phoenix_config())
                ),
            )
            history.ensure_indexes()
            logistics = LogisticsDatasetStore(database)
            logistics.ensure_indexes()
            if args.command == "phoenix-sync":
                records = list(
                    history.evaluations.find(
                        {
                            "$or": [
                                {"phoenix.status": {"$ne": "available"}},
                                {"phoenix": {"$exists": False}},
                            ]
                        }
                    )
                )
                results = []
                for record in records:
                    evidence = history.evaluation_sink(record)
                    history.evaluations.update_one(
                        {"_id": record["_id"]}, {"$set": {"phoenix": evidence}}
                    )
                    results.append(
                        {"evaluation_id": record["evaluation_id"], **evidence}
                    )
                _emit(
                    {
                        "exports": results,
                        "complete": all(
                            item["status"] == "available" for item in results
                        ),
                    }
                )
                return (
                    0 if all(item["status"] == "available" for item in results) else 1
                )
            if args.command == "demo":
                return _run_demo(store, history, logistics, config)
            if args.command == "seed":
                fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
                info = store.materialize(fixture["dataset_id"], fixture["rows"])
                _emit(
                    {
                        "dataset_id": info.dataset_id,
                        "row_count": info.row_count,
                        "content_hash": info.content_hash,
                    }
                )
                return 0
            if args.command == "seed-logistics":
                info = logistics.materialize(
                    "logistics-shipment-threshold-public-v1", **public_incident_bundle()
                )
                _emit(
                    {
                        "dataset_id": info.dataset_id,
                        "input_kind": info.input_kind,
                        "relations": {
                            name: value["row_count"]
                            for name, value in info.relations.items()
                        },
                        "content_hash": info.content_hash,
                    }
                )
                return 0
            if args.command == "eval":
                if args.evaluation_command == "logistics":
                    results = run_logistics_checks(
                        logistics, history, config, PhoenixTelemetry(phoenix_config())
                    )
                    _emit(
                        {
                            "checks": results,
                            "passed": all(item["passed"] for item in results),
                        }
                    )
                    return 0 if all(item["passed"] for item in results) else 1
                return _run_evaluation_command(args, store, config, history)
            if args.command == "final":
                if args.final_command == "reserve":
                    _emit(
                        reserve_cases(
                            store, config, manifest=args.manifest, checkout=Path.cwd()
                        )
                    )
                    return 0
                api_key, model_id = agent_model_config()
                telemetry = PhoenixTelemetry(phoenix_config())
                runner = CandidateRunner(
                    store=store,
                    config=config,
                    history=history,
                    telemetry=telemetry,
                    image=image,
                )
                active = history.active_version(config.task_family)
                if not active:
                    raise FinalAssessmentError(
                        "An accepted active commit is required for final assessment"
                    )
                plan = history.selection_plans.find_one(
                    {"candidate_commit": active["commit"]}
                )
                if not plan or not (
                    history.candidates.find_one({"candidate_id": plan["candidate_id"]})
                    or {}
                ).get("selection", {}).get("accepted"):
                    raise FinalAssessmentError(
                        "Active commit has no accepted selection record"
                    )
                reserved_at = datetime.fromisoformat(
                    json.loads(args.manifest.read_text(encoding="utf-8"))["reserved_at"]
                )
                selected_at = plan["created_at"]
                if selected_at.tzinfo is None:
                    selected_at = selected_at.replace(tzinfo=timezone.utc)
                if reserved_at >= selected_at:
                    raise FinalAssessmentError(
                        "Final cases must be reserved before candidate selection"
                    )
                current_identity = {
                    "candidate_commit": active["commit"],
                    "parent_commit": plan["parent_commit"],
                    "config_hash": config.config_hash,
                    "image": runner.image_identity(),
                    "model": model_identity(OpenRouterModel(api_key, model_id)),
                    "workflows": plan.get("identity", {}).get("workflows", {}),
                }
                if canonical_hash(current_identity) != plan["environment_hash"]:
                    raise FinalAssessmentError(
                        "Model, configuration, or runner image differs from selection"
                    )
                source = CandidateRepository(Path.cwd()).active_checkout(
                    active["commit"]
                )
                results = assess_cases(
                    store,
                    history,
                    config,
                    manifest=args.manifest,
                    checkout=Path.cwd(),
                    run=lambda dataset, question, case_id: runner.run(
                        source=source,
                        source_commit=active["commit"],
                        dataset=dataset,
                        invocation=question,
                        model=OpenRouterModel(api_key, model_id),
                        case_id=case_id,
                        case_exposure="final",
                    ),
                )
                _emit(
                    {
                        "commit": active["commit"],
                        "final_assessments": results,
                        "passed": all(item["passed"] for item in results),
                    }
                )
                return 0 if all(item["passed"] for item in results) else 1
            if args.command == "history":
                return _run_history_command(args, history)
            if args.command == "rollback":
                result = PromotionManager(
                    history, CandidateRepository(Path.cwd())
                ).rollback(
                    task_family=config.task_family,
                    expected_active=args.expected_active,
                    target_commit=args.commit,
                    reason=args.reason,
                )
                _emit(result)
                return 0
            if args.command == "evolve":
                agent_key, agent_id = agent_model_config()
                evolution_key, evolution_id = evolution_model_config()
                telemetry = PhoenixTelemetry(phoenix_config())
                repository = CandidateRepository(Path.cwd())
                observed = history.get_run(args.run_id) or {}
                family = (observed.get("invocation") or {}).get("task_family")
                if family == "logistics-shipment-threshold":
                    if args.rescreen_candidate:
                        raise EvolutionBlocked(
                            "Logistics candidates cannot be rescreened; submit a new incident"
                        )
                    contract = (config.task_contracts or {}).get(
                        "logistics-shipment-threshold-v1"
                    )
                    if not contract:
                        raise EvolutionBlocked("Logistics task contract is unavailable")
                    scoped = replace(
                        config,
                        task_family="logistics-shipment-threshold",
                        task_contract_version="logistics-shipment-threshold-v1",
                        contract_hash=canonical_hash(contract),
                    )
                    controller = LogisticsEvolutionController(
                        logistics=logistics,
                        history=history,
                        config=scoped,
                        telemetry=telemetry,
                        repository=repository,
                        runner=CandidateRunner(
                            store=store,
                            logistics=logistics,
                            config=scoped,
                            history=history,
                            telemetry=telemetry,
                            image=image,
                        ),
                        model_factory=lambda: OpenRouterModel(agent_key, agent_id),
                        evolution_model=OpenRouterModel(
                            evolution_key, evolution_id, timeout_seconds=120
                        ),
                    )
                    result = controller.evolve(args.run_id)
                else:
                    runner = CandidateRunner(
                        store=store,
                        logistics=logistics,
                        config=config,
                        history=history,
                        telemetry=telemetry,
                        image=image,
                    )
                    controller = EvolutionController(
                        store=store,
                        history=history,
                        config=config,
                        telemetry=telemetry,
                        repository=repository,
                        runner=runner,
                        model_factory=lambda: OpenRouterModel(agent_key, agent_id),
                        evolution_model=OpenRouterModel(
                            evolution_key, evolution_id, timeout_seconds=120
                        ),
                    )
                    result = (
                        controller.rescreen(args.run_id, args.rescreen_candidate)
                        if args.rescreen_candidate
                        else controller.evolve(args.run_id)
                    )
                display = dict(result)
                if "selection" in display:
                    selection = dict(display["selection"])
                    selection["trial_count"] = len(selection.pop("trials", []))
                    display["selection"] = selection
                _emit(display)
                return 0 if result["status"] == "activated" else 1
            if args.command == "ui":
                return _run_ui_command(args, store, config, history, logistics)
            task = json.loads(args.task) if args.task is not None else args.question
            if args.task is not None and not isinstance(task, dict):
                raise ValueError("Task must be a JSON object")
            dataset = store.dataset_info(args.dataset)
            api_key, model_id = agent_model_config()
            model = OpenRouterModel(api_key, model_id)
            active = history.active_version(config.task_family)
            if active:
                repository = CandidateRepository(Path.cwd())
                source = repository.active_checkout(active["commit"])
                execution = CandidateRunner(
                    store=store,
                    config=config,
                    history=history,
                    telemetry=PhoenixTelemetry(phoenix_config()),
                    image=image,
                ).run(
                    source=source,
                    source_commit=active["commit"],
                    dataset=dataset,
                    invocation=task,
                    model=model,
                )
            else:
                table = store.open_session(args.dataset)
                execution = RunExecutor(
                    history=history,
                    telemetry=PhoenixTelemetry(phoenix_config()),
                    config=config,
                ).run(
                    model=model,
                    tools=AnalystTools(table, config),
                    dataset=dataset,
                    invocation=task,
                )
            return _present_run(
                execution.result,
                conversational=args.question is not None,
                json_output=args.json,
                execution=execution,
            )
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        DatasetError,
        ScenarioError,
        OracleError,
        HistoryError,
        FinalAssessmentError,
        RunnerError,
        PatchRejected,
        PromotionRejected,
        EvolutionBlocked,
    ) as exc:
        _emit({"error": str(exc)})
        return 2
    except DatabaseError as exc:
        _emit({"error": f"SQLite operation failed: {type(exc).__name__}"})
        return 3
    except OSError as exc:
        _emit({"error": f"File operation failed: {type(exc).__name__}"})
        return 4


if __name__ == "__main__":
    sys.exit(main())
