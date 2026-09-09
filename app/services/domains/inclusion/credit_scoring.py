"""
Alternative-data credit scoring engine.

Purpose: score users with NO formal credit history (gig workers, street
vendors, small farmers) using proxy signals — UPI transaction regularity,
utility bill payment behavior, mobile recharge patterns, GST filings.

This is a transparent, weighted rule-based scorecard (not a black-box
model) deliberately — for fair-lending explainability. A gradient-boosted
model (e.g. XGBoost) can replace `compute_score` later; keep the same
`ScoreResult` contract so the API and explanation layer don't change.
"""
from dataclasses import dataclass

# Weights sum to 1.0 — tuned illustratively, would be validated against
# real default-outcome data in production.
_WEIGHTS = {
    "txn_regularity": 0.25,      # avg_monthly_upi_txn_count, normalized
    "txn_volume": 0.15,          # avg_monthly_upi_volume_inr, normalized
    "bill_payment": 0.25,        # utility_bill_ontime_ratio
    "recharge_regularity": 0.10, # mobile_recharge_regularity_score
    "history_length": 0.10,      # months_of_transaction_history, normalized
    "existing_credit_behavior": 0.15,  # penalizes default flag, rewards clean repayment
}

_SCORE_MIN, _SCORE_MAX = 300, 900


@dataclass
class ScoreResult:
    score: int
    band: str
    signal_breakdown: dict[str, float]


def _normalize(value: float, cap: float) -> float:
    return max(0.0, min(1.0, value / cap))


def _band_for_score(score: int) -> str:
    if score < 500:
        return "poor"
    if score < 650:
        return "fair"
    if score < 750:
        return "good"
    return "excellent"


def compute_score(alt_data: dict) -> ScoreResult:
    txn_regularity = _normalize(alt_data.get("avg_monthly_upi_txn_count", 0), cap=60)
    txn_volume = _normalize(alt_data.get("avg_monthly_upi_volume_inr", 0), cap=30_000)
    bill_payment = alt_data.get("utility_bill_ontime_ratio", 0.0)
    recharge_regularity = alt_data.get("mobile_recharge_regularity_score", 0.0)
    history_length = _normalize(alt_data.get("months_of_transaction_history", 0), cap=24)

    existing_credit_behavior = 1.0
    if alt_data.get("existing_loan_default_flag"):
        existing_credit_behavior = 0.1
    elif alt_data.get("existing_loan_count", 0) > 0:
        existing_credit_behavior = 0.85  # has credit, no default recorded — mildly positive

    weighted_sum = (
        txn_regularity * _WEIGHTS["txn_regularity"]
        + txn_volume * _WEIGHTS["txn_volume"]
        + bill_payment * _WEIGHTS["bill_payment"]
        + recharge_regularity * _WEIGHTS["recharge_regularity"]
        + history_length * _WEIGHTS["history_length"]
        + existing_credit_behavior * _WEIGHTS["existing_credit_behavior"]
    )

    score = round(_SCORE_MIN + weighted_sum * (_SCORE_MAX - _SCORE_MIN))
    score = max(_SCORE_MIN, min(_SCORE_MAX, score))

    breakdown = {
        "txn_regularity": round(txn_regularity, 3),
        "txn_volume": round(txn_volume, 3),
        "bill_payment": round(bill_payment, 3),
        "recharge_regularity": round(recharge_regularity, 3),
        "history_length": round(history_length, 3),
        "existing_credit_behavior": round(existing_credit_behavior, 3),
    }

    return ScoreResult(score=score, band=_band_for_score(score), signal_breakdown=breakdown)
