"""Request validation for the fraud domain."""
from pydantic import BaseModel, Field


class TransactionEvaluateRequest(BaseModel):
    user_ref: str = Field(..., min_length=1, max_length=20)
    txn_ref: str | None = Field(None, max_length=20)
    amount_inr: float = Field(..., gt=0, le=100_000_000)  # positive, capped at 10 crore/txn as a sanity bound
    txn_type: str = Field(..., pattern="^(UPI_OUT|NEFT_OUT|IMPS_OUT|CASH_DEPOSIT)$")
    merchant: str = Field(..., min_length=1, max_length=120)
    location: str = Field(..., min_length=1, max_length=120)
    device_id: str = Field(..., min_length=1, max_length=30)
