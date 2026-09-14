"""Validation tests for the new Pydantic request schemas."""
import pytest
from pydantic import ValidationError

from app.services.domains.lending.schemas import RepaymentRecordRequest
from app.services.domains.collections.schemas import RiskAssessRequest


def test_repayment_record_requires_positive_amount():
    with pytest.raises(ValidationError):
        RepaymentRecordRequest(amount_paid_inr=0, installment_number=1)


def test_repayment_record_requires_installment_at_least_one():
    with pytest.raises(ValidationError):
        RepaymentRecordRequest(amount_paid_inr=1000, installment_number=0)


def test_repayment_record_accepts_valid_payload():
    req = RepaymentRecordRequest(amount_paid_inr=5000, installment_number=2)
    assert req.installment_number == 2


def test_risk_assess_request_defaults_nudge_to_false():
    req = RiskAssessRequest()
    assert req.send_nudge_if_at_risk is False
