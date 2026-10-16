"""Publish protected scores and paired selection trials as Phoenix experiments."""

from __future__ import annotations

import threading
from datetime import timedelta

from self_heal.telemetry import redact_trace_payload


class PhoenixEvaluationPublisher:
    def __init__(self, database, telemetry):
        self.telemetry = telemetry
        self.exports = database["phoenix_exports"]
        self.lock = threading.RLock()

    def __call__(self, record):
        with self.lock:
            annotation = self.telemetry.publish_evaluation(record)
            if annotation["status"] != "available":
                return annotation
            try:
                experiment = self._experiment(record)
                return {**annotation, **experiment}
            except Exception as exc:
                return {"status": "incomplete", "error_type": type(exc).__name__}

    def _experiment(self, record):
        _, client = self.telemetry._dependencies()
        # Every paired plan has one frozen Phoenix dataset. Standalone evals
        # share a single-case dataset, enabling comparison across later runs.
        group = record.get("plan_id") or record["case_id"]
        dataset_key = "dataset:" + group
        saved = self.exports.find_one({"_id": dataset_key})
        if saved is None:
            if record.get("plan_id"):
                plan = self.exports.database["selection_plans"].find_one({"_id": group})
                cases = plan["cases"]
            else:
                cases = [{"case_id": record["case_id"], "dataset": record["dataset"]}]
            dataset = client.datasets.create_dataset(
                name="seal-" + group,
                inputs=[
                    {
                        "case_id": case["case_id"],
                        "dataset_identity": case.get("dataset")
                        or {
                            "id": case.get("dataset_id"),
                            "hash": case.get("dataset_hash"),
                        },
                        "role": case.get("role"),
                    }
                    for case in cases
                ],
                dataset_description="Frozen SEAL evaluation identities; independent oracle results are CODE evaluations.",
            )
            saved = {
                "_id": dataset_key,
                "dataset_id": dataset.id,
                "version_id": dataset.version_id,
                "examples": {
                    example["input"]["case_id"]: example["id"]
                    for example in dataset.examples
                },
            }
            self.exports.insert_one(saved)
        version = (record.get("candidate") or {}).get("version", "current")
        experiment_key = "experiment:" + group + ":" + version
        if not record.get("plan_id"):
            # Phoenix permits one run per example/repetition in an experiment.
            # Standalone executions need their own experiment; retries retain it.
            experiment_key += ":" + record["run_id"]
        experiment = self.exports.find_one({"_id": experiment_key})
        if experiment is None:
            experiment_data = client.experiments.create(
                dataset_id=saved["dataset_id"],
                dataset_version_id=saved["version_id"],
                experiment_name="SEAL " + version,
                repetitions=2,
                experiment_metadata={
                    "group": group,
                    "version": version,
                    "commit": (record.get("candidate") or {}).get("commit"),
                    "environment_hash": record.get("environment_hash"),
                },
            )
            experiment = {"_id": experiment_key, "experiment_id": experiment_data["id"]}
            self.exports.insert_one(experiment)
        run_key = "run:" + record["evaluation_id"]
        run = self.exports.find_one({"_id": run_key})
        end = record["created_at"]
        start = end - timedelta(
            seconds=record.get("resources", {}).get("elapsed_seconds", 0)
        )
        if run is None:
            exported = client.experiments.log_run(
                experiment_id=experiment["experiment_id"],
                dataset_example_id=saved["examples"][record["case_id"]],
                output=redact_trace_payload(
                    {
                        "answer": record.get("actual", {}).get("answer"),
                        "outcome": record.get("actual", {}).get("outcome"),
                    }
                ),
                start_time=start,
                end_time=end,
                repetition_number=record.get("repeat", 0) + 1,
                trace_id=record["trace_id"],
            )
            run = {"_id": run_key, "experiment_run_id": exported["id"]}
            self.exports.insert_one(run)
        client.experiments.log_evaluation(
            experiment_run_id=run["experiment_run_id"],
            name="correctness",
            annotator_kind="CODE",
            score=float(record["passed"]),
            label="pass" if record["passed"] else "fail",
            explanation=record.get("violation")
            or "Independent protected oracle passed",
            start_time=start,
            end_time=end,
            metadata={
                "evaluation_id": record["evaluation_id"],
                "case_id": record["case_id"],
                "resources": record.get("resources"),
            },
        )
        return {
            "dataset_id": saved["dataset_id"],
            "experiment_id": experiment["experiment_id"],
            "experiment_run_id": run["experiment_run_id"],
        }
