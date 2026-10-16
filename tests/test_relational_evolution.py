import json

import pytest
from test_logistics import NoCallsModel, ScriptedModel, materialized_store

from evals.logistics import relational
from evals.logistics.generator import private_evaluation_bundle, public_incident_bundle
from self_heal.controller import EvolutionBlocked
from self_heal.logistics_evolution import LogisticsEvolutionController
from self_heal.model import ModelReply
from self_heal.settings import PhoenixConfig, load_config
from self_heal.storage import HistoryStore
from self_heal.table_store import TableAccessError
from self_heal.telemetry import PhoenixTelemetry


def task(
    relation="customers", aggregate="count", field=None, filters=None, group_by=None
):
    return {
        "operation": "relational_aggregate",
        "relation": relation,
        "aggregate": aggregate,
        "field": field,
        "filters": filters or [],
        "group_by": group_by,
    }


@pytest.mark.parametrize(
    "contract_task,expected",
    [
        (task(), {"value": 8}),
        (task("shipments"), {"value": 74}),
        (task("warehouses", "sum", "warehouse_number"), {"value": 6}),
        (task("warehouses", "avg", "warehouse_number"), {"value": 2}),
        (task("warehouses", "min", "warehouse_number"), {"value": 1}),
        (task("warehouses", "max", "warehouse_number"), {"value": 3}),
        (task("customers", "count_distinct", "segment"), {"value": 1}),
        (task(group_by="status"), {"groups": {"active": 8}}),
        (
            task(filters=[{"field": "status", "operator": "eq", "value": "inactive"}]),
            {"value": 0},
        ),
    ],
)
def test_independent_aggregate_oracle(contract_task, expected):
    assert (
        relational.reference_answer(public_incident_bundle(), contract_task) == expected
    )


@pytest.mark.parametrize(
    "change",
    [
        {"relation": "credentials"},
        {"relation": []},
        {"aggregate": []},
        {"field": "customer_id); DROP TABLE data"},
        {"aggregate": "sum", "field": "customer_id"},
        {"filters": [{"field": "status", "operator": "eq", "value": float("nan")}]},
        {"group_by": {}},
    ],
)
def test_contract_rejects_invalid_or_executable_semantics(change):
    assert relational.validate_task(task() | change) is None


def test_private_entity_counts_vary_to_reject_constant_answers():
    counts = {
        relational.reference_answer(private_evaluation_bundle(seed), task())["value"]
        for seed in range(11)
    }
    assert len(counts) == 11 and 8 not in counts


def test_relation_pages_are_scoped_signed_and_measured():
    store, _, dataset, _ = materialized_store()
    session = store.open_session(dataset.dataset_id)
    page = session.read_relation(relation="customers", limit=3)
    assert len(page["rows"]) == 3 and session.pages_read == 1 and session.bytes_read > 0
    assert not session.completed_relation_scan("customers")
    with pytest.raises(TableAccessError, match="cursor"):
        session.read_relation(relation="shipments", limit=3, cursor=page["next_cursor"])
    seen = page["rows"]
    while page["next_cursor"]:
        page = session.read_relation(
            relation="customers", limit=3, cursor=page["next_cursor"]
        )
        seen += page["rows"]
    assert len(seen) == 8 and session.completed_relation_scan("customers")
    with pytest.raises(TableAccessError):
        session.read_relation(relation="customers", limit=101)


def controller_for(question, replies):
    store, database, dataset, _ = materialized_store()
    history = HistoryStore(database)
    history.ensure_indexes()
    history.runs.insert_one(
        {
            "_id": "incident",
            "run_id": "incident",
            "status": "completed",
            "outcome": "unsupported",
            "limitation_kind": "capability_gap",
            "invocation": {
                "question": question,
                "task_family": "logistics-shipment-threshold",
            },
            "dataset": {
                "id": dataset.dataset_id,
                "content_hash": dataset.content_hash,
                "input_kind": "logistics_bundle",
            },
        }
    )
    controller = LogisticsEvolutionController(
        logistics=store,
        history=history,
        config=load_config(),
        telemetry=PhoenixTelemetry(PhoenixConfig(enabled=False, project="test")),
        repository=None,
        runner=None,
        model_factory=NoCallsModel,
        evolution_model=ScriptedModel(replies),
    )
    return controller


def test_total_customers_reaches_evolution_instead_of_needs_contract(monkeypatch):
    request = task()
    controller = controller_for(
        "How many total customers do we have",
        [
            ModelReply('{"needs_contract":true}'),
            ModelReply(json.dumps(request)),
            ModelReply('{"matches":true}'),
        ],
    )

    class Repository:
        def resolve_commit(self, _commit):
            return "baseline"

    controller.repository = Repository()
    called = []

    def evolve(*args):
        called.append(args)
        return {"status": "activated"}

    monkeypatch.setattr(controller, "_evolve", evolve)
    assert controller.evolve("incident")["status"] == "activated"
    assert called[0][3] == request


def test_misinterpreted_contract_cannot_reach_patch_generation():
    controller = controller_for(
        "How many active customers?",
        [
            ModelReply('{"needs_contract":true}'),
            ModelReply(json.dumps(task())),
            ModelReply('{"matches":false}'),
        ],
    )
    with pytest.raises(EvolutionBlocked, match="semantic verification"):
        controller.evolve("incident")


def test_aggregate_contract_retries_once_after_a_malformed_response(monkeypatch):
    request = task()
    controller = controller_for(
        "How many total customers do we have",
        [
            ModelReply('{"needs_contract":true}'),
            ModelReply('{"relation": "credentials"}'),  # rejected by the validator
            ModelReply(json.dumps(request)),
            ModelReply('{"matches":true}'),
        ],
    )

    class Repository:
        def resolve_commit(self, _commit):
            return "baseline"

    controller.repository = Repository()
    called = []

    def evolve(*args):
        called.append(args)
        return {"status": "activated"}

    monkeypatch.setattr(controller, "_evolve", evolve)
    assert controller.evolve("incident")["status"] == "activated"
    assert called[0][3] == request


def test_aggregate_contract_blocks_after_exhausting_retries():
    controller = controller_for(
        "How many total customers do we have",
        [
            ModelReply('{"needs_contract":true}'),
            ModelReply('{"relation": "credentials"}'),
            ModelReply('{"relation": "credentials"}'),
            ModelReply('{"relation": "credentials"}'),
        ],
    )
    with pytest.raises(EvolutionBlocked, match="Invalid generated relational contract"):
        controller.evolve("incident")


def test_relational_answer_validation_allows_finite_numbers_only():
    from self_heal.runner import _valid_answer
    assert _valid_answer({"value": 1.5}, grouped=False, allow_float=True)
    assert not _valid_answer({"value": 1.5}, grouped=False)
    for value in (True, float("nan"), float("inf"), "1"):
        assert not _valid_answer({"value": value}, grouped=False, allow_float=True)
    assert _valid_answer({"groups": {"A": 1.5}}, grouped=True, allow_float=True)
