"""
Unit tests for LendingService._decide — the deterministic approve/review/
reject rule layer. Instantiated with db=None since _decide() itself never
touches the database (see app/services/domains/lending/service.py); the
service's __init__ only builds boto3 clients, which don't make network
calls at construction time, so this runs fully offline.
"""
import pytest

from app.services.domains.lending.service import LendingService

_svc = LendingService(db=None)


def test_no_prior_credit_score_goes_to_review():
    assert _svc._decide(None, {}) == "under_review"


def test_low_score_without_collateral_is_rejected():
    decision = _svc._decide(400, {"collateral_provided": False})
    assert decision == "rejected"


def test_low_score_with_collateral_is_not_auto_rejected():
    # Collateral de-risks the loan enough that a low score alone shouldn't
    # auto-reject — it should fall through to under_review.
    decision = _svc._decide(400, {"collateral_provided": True})
    assert decision != "rejected"


def test_high_score_with_collateral_is_approved():
    decision = _svc._decide(750, {"collateral_provided": True, "bank_statement_months_provided": 0})
    assert decision == "approved"


def test_high_score_without_supporting_signals_stays_under_review():
    # High score alone, with no collateral, insufficient bank-statement
    # history, and no GST data, should NOT auto-approve.
    decision = _svc._decide(
        750,
        {
            "collateral_provided": False,
            "bank_statement_months_provided": 2,
            "gst_data_available": False,
        },
    )
    assert decision == "under_review"


def test_high_score_with_gst_data_and_short_history_is_approved():
    decision = _svc._decide(
        750,
        {
            "collateral_provided": False,
            "bank_statement_months_provided": 3,
            "gst_data_available": True,
        },
    )
    assert decision == "approved"


def test_mid_range_score_stays_under_review():
    decision = _svc._decide(600, {"collateral_provided": False, "bank_statement_months_provided": 6})
    assert decision == "under_review"
