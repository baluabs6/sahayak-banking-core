"""
Unit tests for FraudDetectionEngine's deterministic rule layer.

Deliberately DB-free — check_transaction()/​_rule_checks() take plain dicts,
so these run without Postgres/Mongo/Redis, unlike the route-level code
which needs a live stack. Covers the rule thresholds specifically, since
those are the auditable/explainable half of the hybrid detector and the
easiest to silently break with an off-by-one.
"""
from datetime import datetime, timezone

import pytest

from app.services.domains.fraud.anomaly_detection import FraudDetectionEngine


def _txn(**overrides) -> dict:
    base = {
        "amount_inr": 1_000.0,
        "timestamp": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "location": "Hyderabad",
        "device_id": "device-known",
        "txn_type": "UPI_OUT",
    }
    base.update(overrides)
    return base


def test_no_history_no_triggers_for_ordinary_transaction():
    engine = FraudDetectionEngine()
    result = engine.check_transaction(_txn(), user_txn_history=[])
    assert result.rule_triggers == []
    assert result.is_flagged is False
    assert result.action == "cleared"


def test_high_value_transaction_triggers_rule():
    engine = FraudDetectionEngine()
    result = engine.check_transaction(_txn(amount_inr=50_000), user_txn_history=[])
    assert "high_value_transaction" in result.rule_triggers
    assert result.is_flagged is True


def test_new_beneficiary_high_value_triggers_auto_block_candidate_rule():
    engine = FraudDetectionEngine()
    result = engine.check_transaction(
        _txn(txn_type="NEFT_OUT", amount_inr=30_000), user_txn_history=[]
    )
    assert "new_beneficiary_high_value" in result.rule_triggers
    assert result.is_flagged is True


def test_unfamiliar_location_only_triggers_when_history_exists():
    engine = FraudDetectionEngine()
    # No history at all -> "unfamiliar location" can't be evaluated, so it
    # must NOT fire (there's nothing to be unfamiliar relative to).
    result_no_history = engine.check_transaction(_txn(location="Mumbai"), user_txn_history=[])
    assert "unfamiliar_location" not in result_no_history.rule_triggers

    history = [_txn(location="Hyderabad")]
    result_with_history = engine.check_transaction(_txn(location="Mumbai"), user_txn_history=history)
    assert "unfamiliar_location" in result_with_history.rule_triggers


def test_velocity_threshold_requires_count_above_limit():
    engine = FraudDetectionEngine()
    below = engine.check_transaction(_txn(), user_txn_history=[], velocity_count=5)
    above = engine.check_transaction(_txn(), user_txn_history=[], velocity_count=6)
    assert "velocity_threshold_exceeded" not in below.rule_triggers
    assert "velocity_threshold_exceeded" in above.rule_triggers
    assert above.is_flagged is True
