"""
Collections domain service.

Real-time repayment-risk nudging: instead of waiting for a loan to go into
default and then starting a recovery process, this watches the same
repayment-schedule and credit-score signals the lending/inclusion domains
already produce and flags a borrower *before* they miss an EMI — early
enough that a proactive nudge (not a collections call) is still the right
response.

Deliberately rule-based and explainable (same philosophy as the fraud and
lending decision layers) rather than a black-box propensity-to-default
model — a risk flag that can't be explained to a borrower or an analyst
isn't useful in a regulated lending context.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import CreditScore, LoanApplication, LoanRepayment
from app.services.aws.cloudwatch_service import CloudWatchService
from app.services.aws.sns_service import SNSService

# An installment is "upcoming" (worth watching) inside this window before
# its due date, and "overdue" any time after due_date with no payment.
_UPCOMING_WINDOW_DAYS = 5


class CollectionsService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.sns = SNSService()
        self.cloudwatch = CloudWatchService()

    async def _credit_score_trend(self, user_id: str) -> str:
        """Compares the two most recent credit scores. A dropping score
        alongside a missed/upcoming EMI is a stronger risk signal than
        either alone."""
        result = await self.db.execute(
            select(CreditScore).where(CreditScore.user_id == user_id).order_by(CreditScore.computed_at.desc()).limit(2)
        )
        scores = result.scalars().all()
        if len(scores) < 2:
            return "insufficient_data"
        latest, previous = scores[0], scores[1]
        if latest.score < previous.score:
            return "declining"
        if latest.score > previous.score:
            return "improving"
        return "stable"

    async def assess_risk(self, application: LoanApplication, send_nudge_if_at_risk: bool = False) -> dict:
        now = datetime.now(timezone.utc)

        repayments_result = await self.db.execute(
            select(LoanRepayment)
            .where(LoanRepayment.loan_application_id == application.id)
            .order_by(LoanRepayment.installment_number)
        )
        repayments = repayments_result.scalars().all()

        overdue = [r for r in repayments if r.status == "pending" and r.due_date < now]
        upcoming = [
            r for r in repayments
            if r.status == "pending" and now <= r.due_date <= now + timedelta(days=_UPCOMING_WINDOW_DAYS)
        ]
        paid_late_count = sum(1 for r in repayments if r.status == "paid_late")

        trend = await self._credit_score_trend(application.user_id)

        reasons: list[str] = []
        if overdue:
            risk_level = "high"
            reasons.append(f"{len(overdue)} installment(s) overdue with no payment recorded")
        elif paid_late_count >= 2 or (trend == "declining" and upcoming):
            risk_level = "medium"
            if paid_late_count >= 2:
                reasons.append(f"{paid_late_count} prior installment(s) paid late")
            if trend == "declining" and upcoming:
                reasons.append("credit score has declined since the last assessment, with an installment due soon")
        elif upcoming:
            risk_level = "low"
            reasons.append(f"{len(upcoming)} installment(s) due within {_UPCOMING_WINDOW_DAYS} days")
        else:
            risk_level = "none"
            reasons.append("no overdue or upcoming installments requiring attention")

        result = {
            "application_ref": application.application_ref,
            "risk_level": risk_level,
            "reasons": reasons,
            "credit_score_trend": trend,
            "overdue_installments": len(overdue),
            "upcoming_installments": len(upcoming),
            "paid_late_installments": paid_late_count,
            "nudge_sent": False,
        }

        try:
            self.cloudwatch.put_metric("RepaymentRiskAssessed", 1, dimensions={"risk_level": risk_level})
        except Exception:
            pass

        if send_nudge_if_at_risk and risk_level in ("medium", "high"):
            try:
                self.sns.publish_repayment_nudge(
                    application.user_id, application.application_ref, risk_level, "; ".join(reasons)
                )
                result["nudge_sent"] = True
            except Exception:
                pass  # non-fatal without live AWS creds — risk assessment itself still returns

        return result

    async def list_at_risk_loans(self, min_risk_level: str = "medium") -> list[dict]:
        """Portfolio-wide scan used by the admin dashboard — every approved
        loan with at least one overdue installment. A real implementation
        would run this as a scheduled batch job rather than on-request; kept
        synchronous here since the dataset is small (dummy/demo scale)."""
        now = datetime.now(timezone.utc)

        apps_result = await self.db.execute(select(LoanApplication).where(LoanApplication.status == "approved"))
        applications = apps_result.scalars().all()

        at_risk: list[dict] = []
        for application in applications:
            assessment = await self.assess_risk(application, send_nudge_if_at_risk=False)
            level_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
            if level_rank.get(assessment["risk_level"], 0) >= level_rank.get(min_risk_level, 2):
                at_risk.append(assessment)
        return at_risk
