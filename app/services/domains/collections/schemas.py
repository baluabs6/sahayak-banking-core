"""Request validation for the collections (repayment-risk) domain."""
from pydantic import BaseModel


class RiskAssessRequest(BaseModel):
    # If true, sends an SNS nudge to the borrower when risk is medium/high.
    # Kept opt-in so an analyst can preview risk without spamming the
    # customer on every assessment call.
    send_nudge_if_at_risk: bool = False
