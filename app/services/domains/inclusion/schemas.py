"""Request/response validation for the inclusion domain. Replaces raw
`payload.get(...)` dict access in routes.py with enforced types/bounds."""
from pydantic import BaseModel, Field, field_validator


class AltDataPayload(BaseModel):
    avg_monthly_upi_txn_count: float = Field(0, ge=0, le=100_000)
    avg_monthly_upi_volume_inr: float = Field(0, ge=0, le=10_000_000)
    utility_bill_ontime_ratio: float = Field(0, ge=0, le=1)
    mobile_recharge_regularity_score: float = Field(0, ge=0, le=1)
    months_of_transaction_history: float = Field(0, ge=0, le=600)
    existing_loan_default_flag: bool = False
    existing_loan_count: int = Field(0, ge=0, le=1000)


class CreditScoreRequest(BaseModel):
    user_ref: str = Field(..., min_length=1, max_length=20)
    alt_data: AltDataPayload
    llm_provider: str | None = Field(None, pattern="^(anthropic|openai|ollama|langchain_auto|gemini|bedrock)$")

    @field_validator("user_ref")
    @classmethod
    def strip_user_ref(cls, v: str) -> str:
        return v.strip()
