"""
Lending domain service (scaffold).
Cash-flow-based underwriting for MSMEs/individuals lacking collateral:
combines the inclusion domain's credit score with loan-specific factors
(GST data availability, bank statement history, collateral) into an
approve/review/reject decision. Notifies via SNS on status change.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import LoanApplication, LoanRepayment, User
from app.services.aws.sns_service import SNSService
from app.services.aws.cloudwatch_service import CloudWatchService

_AUTO_APPROVE_SCORE_THRESHOLD = 700
_AUTO_REJECT_SCORE_THRESHOLD = 450
_DEFAULT_REPAYMENT_TENURE_MONTHS = 6  # scaffold: fixed tenure; production would read tenure from the offer terms


class LendingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.sns = SNSService()
        self.cloudwatch = CloudWatchService()

    def _decide(self, latest_credit_score: int | None, application: dict) -> str:
        """Simple rule-based decision layer. In production this would call
        a trained underwriting model (e.g. XGBoost) taking the credit score
        as one of several features, with a human-in-the-loop for borderline
        cases — this scaffold keeps the rule explicit and auditable.

        Incorporates collateral and GST-data availability alongside the
        credit score and bank-statement history, per the cash-flow-based
        underwriting design described above (previously only the score and
        bank-statement count were checked, silently ignoring the other two
        signals the API/docs already advertised as inputs)."""
        if latest_credit_score is None:
            return "under_review"

        bank_statement_months = application.get("bank_statement_months_provided", 0)
        has_collateral = bool(application.get("collateral_provided", False))
        has_gst_data = bool(application.get("gst_data_available", False))

        if latest_credit_score <= _AUTO_REJECT_SCORE_THRESHOLD and not has_collateral:
            return "rejected"

        # Collateral de-risks the loan enough to relax the bank-statement bar;
        # otherwise require either a longer bank-statement history or GST data
        # as a substitute income-verification signal.
        if latest_credit_score >= _AUTO_APPROVE_SCORE_THRESHOLD and (
            has_collateral
            or bank_statement_months >= 6
            or (has_gst_data and bank_statement_months >= 3)
        ):
            return "approved"

        return "under_review"

    async def submit_application(
        self, user: User, application_data: dict, latest_credit_score: int | None, generate_rationale: bool = False
    ) -> dict:
        decision = self._decide(latest_credit_score, application_data)

        application = LoanApplication(
            id=str(uuid.uuid4()),
            application_ref=f"LN-{uuid.uuid4().hex[:8].upper()}",
            user_id=user.id,
            loan_type=application_data["loan_type"],
            requested_amount_inr=application_data["requested_amount_inr"],
            purpose=application_data.get("purpose", ""),
            collateral_provided=application_data.get("collateral_provided", False),
            gst_data_available=application_data.get("gst_data_available", False),
            bank_statement_months_provided=application_data.get("bank_statement_months_provided", 0),
            status=decision,
        )
        self.db.add(application)
        await self.db.commit()

        if decision == "approved":
            # Generate the EMI schedule immediately on approval so the
            # collections domain has something to monitor from day one,
            # instead of a loan sitting "approved" with no repayment record.
            await self.generate_repayment_schedule(application)

        try:
            self.sns.publish_loan_status_update(user.id, application.application_ref, decision)
            self.cloudwatch.record_loan_decision(decision)
        except Exception:
            pass  # non-fatal without live AWS creds

        rationale = None
        if generate_rationale:
            # RBI-style plain-language rationale for every decision (not just
            # credit scoring) — opt-in via the flag so the fast deterministic
            # path stays free of an LLM call by default; the caller (routes.py)
            # exposes this as `"explain": true` in the request body.
            from app.services.domains.lending.agent import LendingUnderwritingAgent

            agent = LendingUnderwritingAgent(self.db)
            try:
                rationale = await agent.generate_decision_rationale(decision, application_data, latest_credit_score)
            except Exception:
                rationale = None  # best-effort; a rationale-generation failure shouldn't fail the application

        return {
            "application_ref": application.application_ref,
            "status": decision,
            "used_credit_score": latest_credit_score,
            "rationale": rationale,
        }

    async def generate_repayment_schedule(
        self, application: LoanApplication, tenure_months: int = _DEFAULT_REPAYMENT_TENURE_MONTHS
    ) -> list[LoanRepayment]:
        """Equal-installment EMI schedule (principal only, no interest math —
        a real underwriting engine would price interest into amount_due_inr;
        this scaffold keeps the schedule auditable and simple). Idempotent:
        if a schedule already exists for this application, returns it
        unchanged rather than double-scheduling."""
        existing = await self.db.execute(
            select(LoanRepayment).where(LoanRepayment.loan_application_id == application.id)
        )
        rows = existing.scalars().all()
        if rows:
            return list(rows)

        installment_amount = round(float(application.requested_amount_inr) / tenure_months, 2)
        now = datetime.now(timezone.utc)
        schedule: list[LoanRepayment] = []
        for i in range(1, tenure_months + 1):
            schedule.append(
                LoanRepayment(
                    id=str(uuid.uuid4()),
                    repayment_ref=f"RPY-{uuid.uuid4().hex[:8].upper()}",
                    loan_application_id=application.id,
                    installment_number=i,
                    amount_due_inr=installment_amount,
                    due_date=now + timedelta(days=30 * i),
                    status="pending",
                )
            )
        self.db.add_all(schedule)
        await self.db.commit()
        return schedule

    async def record_repayment(self, application: LoanApplication, installment_number: int, amount_paid_inr: float) -> dict:
        """Marks a specific installment paid. A real payments integration
        would call this from a webhook (UPI/NACH mandate settlement); here
        it's exposed directly so an analyst/customer can record a payment."""
        result = await self.db.execute(
            select(LoanRepayment).where(
                LoanRepayment.loan_application_id == application.id,
                LoanRepayment.installment_number == installment_number,
            )
        )
        repayment = result.scalar_one_or_none()
        if repayment is None:
            return {"error": "No repayment schedule entry found for that installment_number"}

        now = datetime.now(timezone.utc)
        repayment.amount_paid_inr = amount_paid_inr
        repayment.paid_at = now
        repayment.status = "paid_late" if now > repayment.due_date else "paid"
        await self.db.commit()

        return {
            "repayment_ref": repayment.repayment_ref,
            "installment_number": repayment.installment_number,
            "status": repayment.status,
            "amount_paid_inr": float(repayment.amount_paid_inr),
            "due_date": repayment.due_date.isoformat(),
            "paid_at": repayment.paid_at.isoformat(),
        }

    async def list_repayments(self, application: LoanApplication) -> list[dict]:
        result = await self.db.execute(
            select(LoanRepayment)
            .where(LoanRepayment.loan_application_id == application.id)
            .order_by(LoanRepayment.installment_number)
        )
        rows = result.scalars().all()
        return [
            {
                "repayment_ref": r.repayment_ref,
                "installment_number": r.installment_number,
                "amount_due_inr": float(r.amount_due_inr),
                "due_date": r.due_date.isoformat(),
                "amount_paid_inr": float(r.amount_paid_inr) if r.amount_paid_inr is not None else None,
                "paid_at": r.paid_at.isoformat() if r.paid_at else None,
                "status": r.status,
            }
            for r in rows
        ]
