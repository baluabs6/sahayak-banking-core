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
_RULE_VELOCITY_PER_MINUTE_THRESHOLD = 5  # >5 txns/minute from one user is a classic card-testing/bot pattern


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
        # Range of decision_function scores observed on the training data,
        # used to normalize a raw score into 0..1 (see fit()/check_transaction).
        self._train_score_range: tuple[float, float] | None = None

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
        """Fits on real per-row features, not placeholders.

        Previously amount_ratio and is_new_device were hardcoded to 1.0/0.0
        for every training row — the model saw zero variance on 2 of its 4
        features during training, then scored live transactions (in
        check_transaction) on all 4, so those two dimensions were effectively
        dead weight in the trained model. Here each transaction's features
        are computed against the transactions that precede it chronologically
        (the same "prior history" logic used at scoring time), so the model
        actually learns the amount_ratio/new_device distributions it will be
        scored against.
        """
        if not historical_transactions:
            self._model = None
            return

        def _sort_key(t: dict):
            ts = t["timestamp"]
            return ts if not isinstance(ts, str) else ts

        ordered = sorted(historical_transactions, key=_sort_key)

        rows = []
        for i, t in enumerate(ordered):
            prior = ordered[:i]  # only transactions strictly before this one
            rows.append(self._featurize(t, prior)[0])
        features = np.array(rows)

        self._model = IsolationForest(n_estimators=100, contamination=0.15, random_state=42)
        self._model.fit(features)

        # Calibrate the anomaly-score scale against this training data instead
        # of a fixed constant. sklearn's decision_function is not bounded to a
        # fixed range — it's calibrated per-dataset around a contamination-set
        # threshold at 0, and can go negative for perfectly ordinary points.
        # The old formula `(0.5 - raw) * 2` assumed raw always fell in [0, 0.5],
        # which isn't guaranteed, so genuinely normal transactions could — and
        # did — score a maxed-out 1.0. Storing the observed training range lets
        # check_transaction do a proper min-max normalization instead.
        train_scores = self._model.decision_function(features)
        lo, hi = float(train_scores.min()), float(train_scores.max())
        if hi - lo < 1e-9:  # degenerate: all-identical training rows
            hi = lo + 1e-9
        self._train_score_range = (lo, hi)

    def _rule_checks(self, txn: dict, user_txn_history: list[dict], velocity_count: int | None = None) -> list[str]:
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

        # velocity_count is the live per-minute counter from DynamoDB (see
        # DynamoDBService.increment_txn_velocity/get_txn_velocity). Previously
        # this counter was written on every transaction but never read by any
        # rule, so no amount of rapid-fire transactions from one user could
        # ever trigger a flag on velocity alone.
        if velocity_count is not None and velocity_count > _RULE_VELOCITY_PER_MINUTE_THRESHOLD:
            triggers.append("velocity_threshold_exceeded")

        return triggers

    def check_transaction(self, txn: dict, user_txn_history: list[dict], velocity_count: int | None = None) -> FraudCheckResult:
        rule_triggers = self._rule_checks(txn, user_txn_history, velocity_count)

        ml_score = 0.0
        if self._model is not None and self._train_score_range is not None:
            features = self._featurize(txn, user_txn_history)
            # decision_function: higher = more normal, lower = more anomalous.
            # Normalize against the range actually observed on training data
            # (calibrated per-dataset in fit(), see the comment there) rather
            # than a fixed constant — a point at or beyond the most anomalous
            # training example scores 1.0; a point at or beyond the most
            # normal training example scores 0.0.
            raw = float(self._model.decision_function(features)[0])
            lo, hi = self._train_score_range
            ml_score = float(np.clip((hi - raw) / (hi - lo), 0.0, 1.0))

        is_flagged = bool(rule_triggers) or ml_score >= 0.7
        action = "cleared"
        if is_flagged:
            action = "auto_blocked" if ("new_beneficiary_high_value" in rule_triggers and ml_score >= 0.6) else "flagged_for_review"

        return FraudCheckResult(is_flagged=is_flagged, ml_anomaly_score=round(ml_score, 4), rule_triggers=rule_triggers, action=action)
