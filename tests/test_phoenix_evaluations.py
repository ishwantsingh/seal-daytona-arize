from datetime import datetime, timezone
from types import SimpleNamespace

from self_heal.database import SQLiteDatabase
from self_heal.phoenix_evaluations import PhoenixEvaluationPublisher


class Client:
    def __init__(self):
        self.datasets = self
        self.experiments = self
        self.dataset_creations = []
        self.experiment_creations = []
        self.runs = []
        self.scores = []

    def create_dataset(self, **kwargs):
        self.dataset_creations.append(kwargs)
        return SimpleNamespace(
            id="dataset-1",
            version_id="version-1",
            examples=[
                {"id": f"example-{i}", "input": value}
                for i, value in enumerate(kwargs["inputs"])
            ],
        )

    def create(self, **kwargs):
        self.experiment_creations.append(kwargs)
        return {"id": f"experiment-{len(self.experiment_creations)}"}

    def log_run(self, **kwargs):
        self.runs.append(kwargs)
        return {"id": f"run-{len(self.runs)}"}

    def log_evaluation(self, **kwargs):
        self.scores.append(kwargs)


class Telemetry:
    def __init__(self, client):
        self.client = client
        self.status = "available"

    def _dependencies(self):
        return None, self.client

    def publish_evaluation(self, record):
        return {"status": self.status, "trace_id": record["trace_id"]}


def record(version, identifier):
    return {
        "_id": identifier,
        "evaluation_id": identifier,
        "case_id": "case-1",
        "plan_id": "plan-1",
        "dataset": {"id": "data-1", "content_hash": "hash"},
        "candidate": {"version": version, "commit": version + "-sha"},
        "trace_id": "a" * 32,
        "created_at": datetime.now(timezone.utc),
        "passed": version == "candidate",
        "actual": {"answer": {"value": 18}, "outcome": "answered"},
        "violation": None,
        "resources": {"elapsed_seconds": 1},
        "repeat": 0,
    }


def setup():
    database = SQLiteDatabase()
    database.selection_plans.insert_one(
        {
            "_id": "plan-1",
            "cases": [
                {
                    "case_id": "case-1",
                    "dataset_id": "data-1",
                    "dataset_hash": "hash",
                    "role": "original",
                }
            ],
        }
    )
    client = Client()
    telemetry = Telemetry(client)
    return PhoenixEvaluationPublisher(database, telemetry), client, telemetry


def test_paired_trials_share_one_dataset_and_have_separate_experiments():
    publisher, client, _ = setup()
    baseline = publisher(record("baseline", "eval-1"))
    candidate = publisher(record("candidate", "eval-2"))
    assert baseline["status"] == candidate["status"] == "available"
    assert baseline["dataset_id"] == candidate["dataset_id"]
    assert baseline["experiment_id"] != candidate["experiment_id"]
    assert len(client.dataset_creations) == 1 and len(client.experiment_creations) == 2
    assert [score["score"] for score in client.scores] == [0.0, 1.0]
    assert all(score["annotator_kind"] == "CODE" for score in client.scores)
    assert client.runs[0]["output"]["answer"] == {"redacted_answer_fields": ["value"]}


def test_retry_reuses_remote_run_and_republishes_score_without_agent_execution():
    publisher, client, _ = setup()
    first = publisher(record("candidate", "eval-1"))
    second = publisher(record("candidate", "eval-1"))
    assert first["experiment_run_id"] == second["experiment_run_id"]
    assert len(client.runs) == 1 and len(client.scores) == 2


def test_partial_publication_failure_remains_retryable():
    publisher, client, _ = setup()
    original = client.log_evaluation
    client.log_evaluation = lambda **kwargs: (_ for _ in ()).throw(
        ConnectionError("offline")
    )
    assert publisher(record("candidate", "eval-1"))["status"] == "incomplete"
    client.log_evaluation = original
    assert publisher(record("candidate", "eval-1"))["status"] == "available"
    assert len(client.runs) == 1


def test_missing_trace_does_not_create_an_experiment():
    publisher, client, telemetry = setup()
    telemetry.status = "incomplete"
    assert publisher(record("candidate", "eval-1"))["status"] == "incomplete"
    assert not client.dataset_creations
