"""Real Daytona transport smoke test with a deterministic logistics task."""

import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from self_heal.database import SQLiteDatabase
from self_heal.settings import load_config, PhoenixConfig
from self_heal.runner import CandidateRunner
from self_heal.table_store import TableStore
from self_heal.logistics_store import LogisticsDatasetStore
from self_heal.telemetry import PhoenixTelemetry
from evals.logistics.generator import public_incident_bundle
from harness.logistics import LogisticsTools

config = load_config()
with SQLiteDatabase() as database:
    store = TableStore(database, config)
    logistics = LogisticsDatasetStore(database)
    logistics.ensure_indexes()
    dataset = logistics.materialize("smoke", **public_incident_bundle())
    table = logistics.open_session(dataset.dataset_id)
    runner = CandidateRunner(
        store=store,
        logistics=logistics,
        config=config,
        history=None,
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False)),
    )
    result = runner._execute_sandbox(
        Path("harness").resolve(),
        "How many customers sent more than 15 shipments from warehouse 3 yesterday?",
        "daytona-live-smoke",
        table,
        object(),
        LogisticsTools(table, config),
        input_kind="logistics_bundle",
    )
    print(
        json.dumps(
            {
                "outcome": result.outcome,
                "answer": result.answer,
                "error": result.error,
                "pages": result.table_pages,
                "elapsed_seconds": result.elapsed_seconds,
                "sandbox_id": runner.last_sandbox_id,
                "cleanup_error": runner.cleanup_error,
            }
        )
    )
    assert result.outcome == "answered" and result.answer == {"value": 2}, result.error
