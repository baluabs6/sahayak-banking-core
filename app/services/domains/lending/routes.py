"""
Lending domain routes — BlackSheep Router.
Mounted at /api/v1/lending in app/main.py.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
from pydantic import ValidationError
from sqlalchemy import select

from app.core.audit import log_audit_event
from app.core.auth_middleware import ensure_owner_or_role, require_identity
from app.db.postgres import AsyncSessionLocal
from app.db.redis_client import check_rate_limit
from app.models.postgres_models import CreditScore, LoanApplication, User
from app.services.domains.lending.schemas import LoanApplicationRequest
from app.services.domains.lending.service import LendingService

_ANALYST_ROLES = ("analyst", "admin")


class LendingController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/lending"

    @post("/applications")
    async def submit_application(self, request: Request) -> json:
        """Body: { "user_ref": "USR-1005", "loan_type": "msme_working_capital",
        "requested_amount_inr": 300000, "purpose": "...", "collateral_provided": false,
        "gst_data_available": true, "bank_statement_months_provided": 12,
        "explain": true (optional — generates an RBI-style plain-language rationale) }"""
        identity = require_identity(request)

        raw = await request.json()
        try:
            body = LoanApplicationRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        deny = ensure_owner_or_role(identity, body.user_ref, allowed_roles=_ANALYST_ROLES)
        if deny:
            return deny

        allowed, _ = await check_rate_limit(identity["sub"], route="lending_apply", limit=10, window_seconds=60)
        if not allowed:
            return json({"error": "Rate limit exceeded. Try again shortly."}, status=429)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_ref == body.user_ref))
            user = user_result.scalar_one_or_none()
            if user is None:
                return json({"error": "Unknown user_ref"}, status=404)

            latest_score_result = await db.execute(
                select(CreditScore).where(CreditScore.user_id == user.id).order_by(CreditScore.computed_at.desc()).limit(1)
            )
            latest_score = latest_score_result.scalar_one_or_none()

            service = LendingService(db)
            result = await service.submit_application(
                user,
                body.model_dump(),
                latest_credit_score=latest_score.score if latest_score else None,
                generate_rationale=body.explain,
            )

        await log_audit_event(
            action="loan.decision", actor_ref=identity["sub"], actor_role=identity.get("role"),
            target_user_ref=body.user_ref,
            details={"application_ref": result.get("application_ref"), "status": result.get("status")},
        )
        return json(result)

    @post("/applications/{application_ref}/agent-review")
    async def agent_review(self, application_ref: str) -> json:
        """Autonomous underwriting agent — advisory only, for applications
        currently sitting in 'under_review'. Pulls credit score, transaction
        velocity, and loan history via tool calls and returns a
        recommendation; does NOT change the application's status. See
        app/services/domains/lending/agent.py for why this stays advisory."""
        from app.services.domains.lending.agent import LendingUnderwritingAgent

        async with AsyncSessionLocal() as db:
            app_result = await db.execute(select(LoanApplication).where(LoanApplication.application_ref == application_ref))
            application = app_result.scalar_one_or_none()
            if application is None:
                return json({"error": "Unknown application_ref"}, status=404)
            if application.status != "under_review":
                return json({"error": f"Application status is '{application.status}', not 'under_review' — agent review is for borderline cases only"}, status=400)

            user_result = await db.execute(select(User).where(User.id == application.user_id))
            user = user_result.scalar_one()

            agent = LendingUnderwritingAgent(db)
            application_dict = {
                "loan_type": application.loan_type,
                "requested_amount_inr": float(application.requested_amount_inr),
                "purpose": application.purpose,
                "collateral_provided": application.collateral_provided,
                "gst_data_available": application.gst_data_available,
                "bank_statement_months_provided": application.bank_statement_months_provided,
            }
            review = await agent.review_application(user, application_dict)
            return json({"application_ref": application_ref, **review})

    @post("/applications/{application_ref}/document-request")
    async def document_request(self, application_ref: str) -> json:
        """Document-collection agent: for under_review applications, drafts
        the customer-facing message requesting whatever KYC/income docs
        appear to be missing."""
        from app.services.domains.lending.agent import LendingUnderwritingAgent

        async with AsyncSessionLocal() as db:
            app_result = await db.execute(select(LoanApplication).where(LoanApplication.application_ref == application_ref))
            application = app_result.scalar_one_or_none()
            if application is None:
                return json({"error": "Unknown application_ref"}, status=404)

            user_result = await db.execute(select(User).where(User.id == application.user_id))
            user = user_result.scalar_one()

            agent = LendingUnderwritingAgent(db)
            application_dict = {
                "loan_type": application.loan_type,
                "collateral_provided": application.collateral_provided,
                "gst_data_available": application.gst_data_available,
                "bank_statement_months_provided": application.bank_statement_months_provided,
            }
            draft = await agent.draft_document_request(user, application_dict)
            return json({"application_ref": application_ref, "draft_message": draft})
