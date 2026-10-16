from pathlib import Path

from self_heal.database import SQLiteDatabase
import pytest

from self_heal.evaluation import SelectionDecision
from self_heal.promotion import PromotionManager, PromotionRejected
from self_heal.repository import CandidateSource
from self_heal.storage import HistoryStore
from self_heal.contracts import utc_now


class Repository:
    def inspect(self, source):
        assert source.candidate_commit == "child"

    def resolve_commit(self, commit):
        return commit


def setup():
    history = HistoryStore(SQLiteDatabase())
    history.ensure_indexes()
    history.record_candidate({
        "_id": "candidate", "candidate_id": "candidate", "candidate_commit": "child",
        "parent_commit": "parent", "task_family": "inventory-totals",
        "changed_mechanism": "aggregate", "created_at": utc_now(),
    })
    history.record_selection_plan({
        "_id": "plan", "candidate_id": "candidate", "candidate_commit": "child",
        "parent_commit": "parent", "config_hash": "config",
        "environment_hash": "environment", "cases": [{"case_id": "original"}],
        "created_at": utc_now(),
    })
    history.record_candidate_result("candidate", {"accepted": True, "plan_id": "plan"})
    source = CandidateSource("parent", "child", Path("/candidate"), "diff", ("harness/tools.py",))
    decision = SelectionDecision(True, (), "plan", "environment", "child", "parent", ())
    return history, PromotionManager(history, Repository()), source, decision


def test_exact_tested_commit_activates_and_can_roll_back():
    history, promotion, source, decision = setup()
    active = promotion.activate(task_family="inventory-totals", candidate_id="candidate",
                                source=source, decision=decision,
                                current_environment_hash="environment")
    assert active["active_commit"] == "child"
    assert history.active_version("inventory-totals")["commit"] == "child"
    restored = promotion.rollback(task_family="inventory-totals", expected_active="child",
                                  target_commit="parent", reason="Regression observed")
    assert restored["active_commit"] == "parent"
    assert history.active_version("inventory-totals")["commit"] == "parent"


def test_changed_environment_and_stale_parent_are_rejected():
    history, promotion, source, decision = setup()
    with pytest.raises(PromotionRejected, match="environment"):
        promotion.activate(task_family="inventory-totals", candidate_id="candidate",
                           source=source, decision=decision,
                           current_environment_hash="different")
    history.active_versions.insert_one({"_id": "inventory-totals", "commit": "other"})
    with pytest.raises(PromotionRejected, match="parent changed"):
        promotion.activate(task_family="inventory-totals", candidate_id="candidate",
                           source=source, decision=decision,
                           current_environment_hash="environment")
