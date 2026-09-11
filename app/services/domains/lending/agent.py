"""
Lending — Autonomous Underwriting Agent.

Replaces the fixed if/else scorecard's *reasoning* (not its guardrails) with
an explicit tool-calling agent that pulls the credit score, recent
transaction velocity, and loan history itself, then reasons over them —
while `LendingService._decide` (service.py) remains the deterministic,
auditable floor: the agent's recommendation is advisory and always passes
through `_decide`'s hard reject/approve thresholds before anything is
persisted. This keeps the fair-lending guarantee (a customer can always be
shown the exact rule that drove a reject) while adding agentic reasoning
for the "under_review" band where a fixed rule genuinely can't capture
enough nuance.

Three capabilities, per the brief:
  1. Tool-calling underwriter — reasons over score + velocity + history.
  2. Adverse-action / approval rationale — RBI-style plain-language
     explanation for every decision, reusing the fair-lending explanation
     pattern from inclusion/service.py's `explain_credit_decision`.
  3. Document-collection agent — for "review" cases, identifies missing
     KYC/income docs and drafts the customer-facing request.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import LoanApplication, Transaction, User
from app.services.aws.dynamodb_service import DynamoDBService
from app.services.ai.agent_orchestrator import AgentOrchestrator, Tool, log_agent_run
from app.services.ai.llm_provider import get_llm_provider

_UNDERWRITER_SYSTEM_PROMPT = (
    "You are an underwriting analyst assistant for an Indian MSME/personal "
    "lending platform reviewing a borderline ('under_review') loan application. "
    "Use the tools available to look up the applicant's credit score, recent "
    "transaction velocity, and loan history before forming an opinion — never "
    "guess at data you can look up. Recommend one of: 'approve', 'reject', or "
    "'keep_under_review' (if you genuinely need a human underwriter, e.g. "
    "conflicting signals). You are advisory only: a human/deterministic layer "
    "makes the final call. End with a short, plain-language rationale citing "
    "the specific factors you weighed."
)


class LendingUnderwritingAgent:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.dynamodb = DynamoDBService()

    def _build_tools(self, user: User) -> list[Tool]:
        async def get_credit_score(_input: dict) -> dict:
            from app.models.postgres_models import CreditScore

            result = await self.db.execute(
                select(CreditScore).where(CreditScore.user_id == user.id).order_by(CreditScore.computed_at.desc()).limit(1)
            )
            score = result.scalar_one_or_none()
            if score is None:
                return {"score": None, "band": None, "note": "No credit score on file for this user."}
            return {"score": score.score, "band": score.score_band, "computed_at": score.computed_at.isoformat()}

        async def get_recent_transaction_velocity(_input: dict) -> dict:
            from datetime import datetime, timezone

            window_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            try:
                count = self.dynamodb.get_txn_velocity(user.id, window_key)
            except Exception:
                count = None
            return {"txns_in_current_minute_window": count, "note": "None means the signal was unavailable (e.g. no live AWS credentials in this environment)."}

        async def get_loan_history(_input: dict) -> dict:
            result = await self.db.execute(
                select(LoanApplication).where(LoanApplication.user_id == user.id).order_by(LoanApplication.submitted_at.desc()).limit(10)
            )
            applications = result.scalars().all()
            return {
                "prior_applications": [
                    {
                        "application_ref": a.application_ref,
                        "loan_type": a.loan_type,
                        "requested_amount_inr": float(a.requested_amount_inr),
                        "status": a.status,
                        "submitted_at": a.submitted_at.isoformat(),
                    }
                    for a in applications
                ]
            }

        return [
            Tool(
                name="get_credit_score",
                description="Fetch the applicant's most recent inclusion credit score and band.",
                input_schema={"type": "object", "properties": {}},
                handler=get_credit_score,
            ),
            Tool(
                name="get_recent_transaction_velocity",
                description="Fetch how many transactions the applicant has made in the current per-minute window (fraud/velocity signal).",
                input_schema={"type": "object", "properties": {}},
                handler=get_recent_transaction_velocity,
            ),
            Tool(
                name="get_loan_history",
                description="Fetch the applicant's prior loan applications and their outcomes.",
                input_schema={"type": "object", "properties": {}},
                handler=get_loan_history,
            ),
        ]

    async def review_application(self, user: User, application: dict) -> dict:
        """Runs the agent for a borderline application. Returns the agent's
        advisory recommendation + rationale + full audit trace; does NOT
        itself change the application's status — see the module docstring."""
        tools = self._build_tools(user)
        orchestrator = AgentOrchestrator(tools=tools, system_prompt=_UNDERWRITER_SYSTEM_PROMPT)

        user_prompt = (
            f"Applicant: {user.user_ref}, occupation: {user.occupation}, region: {user.region}.\n"
            f"Loan application: {application}\n"
            "Look up their credit score, recent transaction velocity, and loan history, "
            "then recommend approve / reject / keep_under_review with a short rationale."
        )
        result = await orchestrator.run(user_prompt)
        await log_agent_run("lending_underwriting_agent", user.id, result, extra={"application": application})

        return {
            "recommendation_text": result.final_text,
            "tool_calls": [{"tool": t.tool_name, "input": t.tool_input} for t in result.trace],
        }

    async def generate_decision_rationale(self, decision: str, application: dict, credit_score: int | None) -> str:
        """RBI-style plain-language explanation for ANY lending decision
        (approve/reject/review) — extends the fair-lending explanation
        pattern already used for credit scoring (inclusion/service.py) to
        every lending outcome, not just the credit score that feeds it."""
        provider = get_llm_provider()
        system = (
            "You write plain-language, respectful explanations of loan decisions for "
            "Indian retail/MSME borrowers, in the spirit of RBI fair-lending practices: "
            "no technical jargon, cite 2-3 concrete factors, and if the decision is a "
            "rejection or review, be clear about what would need to change."
        )
        user_prompt = (
            f"Decision: {decision}\n"
            f"Credit score used: {credit_score}\n"
            f"Application details: {application}\n"
            "Write a 3-4 sentence explanation for the applicant."
        )
        return await provider.complete(system, user_prompt)

    async def draft_document_request(self, user: User, application: dict) -> str:
        """For 'under_review' cases: identifies which additional KYC/income
        documents are likely missing and drafts the customer-facing request
        message — a document-collection agent, per the brief."""
        provider = get_llm_provider()
        system = (
            "You are a loan-processing assistant for an Indian MSME/personal lending "
            "platform. Given an under-review application, identify which of the "
            "following the applicant may be missing and draft a short, polite message "
            "requesting them: GST registration/filings, bank statements (aim for 6+ "
            "months), KYC documents (Aadhaar, PAN), collateral documentation. Only ask "
            "for what's actually missing or thin based on the application data given — "
            "don't request documents already provided."
        )
        user_prompt = (
            f"Applicant: {user.user_ref}\n"
            f"Application: {application}\n"
            "Draft the document-request message."
        )
        return await provider.complete(system, user_prompt)
