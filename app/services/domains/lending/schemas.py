"""Request validation for the lending domain."""
from pydantic import BaseModel, Field


class LoanApplicationRequest(BaseModel):
    user_ref: str = Field(..., min_length=1, max_length=20)
    loan_type: str = Field(..., pattern="^(personal|msme_working_capital|agri_credit|microfinance)$")
    requested_amount_inr: float = Field(..., gt=0, le=100_000_000)
    purpose: str = Field("", max_length=120)
    collateral_provided: bool = False
    gst_data_available: bool = False
    bank_statement_months_provided: int = Field(0, ge=0, le=600)
