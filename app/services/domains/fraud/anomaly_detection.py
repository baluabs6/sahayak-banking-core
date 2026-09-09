"""
Fraud detection engine — hybrid approach:
  1. Deterministic rules (fast, explainable, catch known patterns)
  2. ML anomaly scoring via Isolation Forest (catches novel patterns)
Both run on every transaction; either can trigger a flag.
"""
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import IsolationForest

# Simple in-memory "known geography" baseline per user for the demo.
# In production this is a feature store (e.g. Feast) backed by DynamoDB/Redis.
_RULE_HIGH_VALUE_THRESHOLD_INR = 40_000
_RULE_NEW_BENEFICIARY_TYPES = {"NEFT_OUT", "IMPS_OUT"}


@dataclass
class FraudCheckResult:
    is_flagged: bool
    ml_anomaly_score: float
    rule_triggers: list[str] = field(default_factory=list)
    action: str = "cleared"  # cleared | flagged_for_review | auto_blocked


class FraudDetectionEngine:
    def __init__(self) -> None:
        # Trained lazily on the transaction dataset provided; in production
        # this is a scheduled retraining job writing model artifacts to S3.
        self._model: IsolationForest | None = None

    def _featurize(self, txn: dict, user_txn_history: list[dict]) -> np.ndarray:
        amount = float(txn["amount_inr"])
        hour = int(txn["timestamp"][11:13]) if isinstance(txn["timestamp"], str) else txn["timestamp"].hour
        historical_avg = (
            np.mean([t["amount_inr"] for t in user_txn_history]) if user_txn_history else amount
        )
        amount_ratio = amount / (historical_avg + 1e-6)
        is_new_device = 1.0 if txn.get("device_id") not in {t.get("device_id") for t in user_txn_history} else 0.0
        return np.array([[amount, hour, amount_ratio, is_new_device]])

    def fit(self, historical_transactions: list[dict]) -> None:
        if not historical_transactions:
            self._model = None
            return
        features = np.array(
            [
                [
                    float(t["amount_inr"]),
                    int(t["timestamp"][11:13]) if isinstance(t["timestamp"], str) else t["timestamp"].hour,
                    1.0,
                    0.0,
                ]
                for t in historical_transactions
            ]
        )
        self._model = IsolationForest(n_estimators=100, contamination=0.15, random_state=42)
        self._model.fit(features)

    def _rule_checks(self, txn: dict, user_txn_history: list[dict]) -> list[str]:
        triggers: list[str] = []

        if float(txn["amount_inr"]) >= _RULE_HIGH_VALUE_THRESHOLD_INR:
            triggers.append("high_value_transaction")

        known_locations = {t["location"] for t in user_txn_history}
        if known_locations and txn["location"] not in known_locations:
            triggers.append("unfamiliar_location")

        if txn["txn_type"] in _RULE_NEW_BENEFICIARY_TYPES and float(txn["amount_inr"]) >= 25_000:
            triggers.append("new_beneficiary_high_value")

        known_devices = {t["device_id"] for t in user_txn_history}
        if known_devices and txn["device_id"] not in known_devices:
            triggers.append("new_device")

        return triggers

    def check_transaction(self, txn: dict, user_txn_history: list[dict]) -> FraudCheckResult:
        rule_triggers = self._rule_checks(txn, user_txn_history)

        ml_score = 0.0
        if self._model is not None:
            features = self._featurize(txn, user_txn_history)
            # decision_function: higher = more normal. Convert to a 0-1 anomaly score.
            raw = self._model.decision_function(features)[0]
            ml_score = float(np.clip((0.5 - raw) * 2, 0.0, 1.0))

        is_flagged = bool(rule_triggers) or ml_score >= 0.7
        action = "cleared"
        if is_flagged:
            action = "auto_blocked" if ("new_beneficiary_high_value" in rule_triggers and ml_score >= 0.6) else "flagged_for_review"

        return FraudCheckResult(is_flagged=is_flagged, ml_anomaly_score=round(ml_score, 4), rule_triggers=rule_triggers, action=action)
