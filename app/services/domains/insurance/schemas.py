"""Request validation for the insurance domain."""
from pydantic import BaseModel, Field


class ClaimFileRequest(BaseModel):
    user_ref: str = Field(..., min_length=1, max_length=20)
    policy_type: str = Field(..., pattern="^(crop_insurance|health|shop_property)$")
    trigger_event: str = Field("", max_length=60)
    region: str = Field("", max_length=80)
    claim_amount_inr: float = Field(..., gt=0, le=100_000_000)
    parametric_data_source: str | None = Field(None, max_length=60)
    index_value: float | None = None
    index_threshold: float | None = None
