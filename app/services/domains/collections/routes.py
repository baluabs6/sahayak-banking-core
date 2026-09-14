"""
Collections domain routes — BlackSheep Router.
Mounted at /api/v1/collections in app/main.py.

Real-time repayment-risk nudging (see app/services/domains/collections/service.py).
Analyst/admin only — this is an ops/risk tool, not something a customer
calls on their own loan.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, get, post
from pydantic import ValidationError
from sqlalchemy import select

from app.core.audit import log_audit_event
from app.core.auth_middleware import require_identity
from app.db.postgres import AsyncSessionLocal
from app.models.postgres_models import LoanApplication
from app.services.domains.collections.schemas import RiskAssessRequest
from app.services.domains.collections.service import CollectionsService

_ANALYST_ROLES = ("analyst", "admin")


class CollectionsController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/collections"

    @post("/loans/{application_ref}/assess")
    async def assess_loan_risk(self, request: Request, application_ref: str) -> json:
        """Body (optional): { "send_nudge_if_at_risk": true }"""
        identity = require_identity(request)
        if identity.get("role") not in _ANALYST_ROLES:
            return json({"error": "Forbidden: repayment-risk assessment requires an analyst/admin token."}, status=403)

        raw = {}
        try:
            raw = await request.json()
        except Exception:
            raw = {}
        try:
            body = RiskAssessRequest.model_validate(raw or {})
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        async with AsyncSessionLocal() as db:
            app_result = await db.execute(select(LoanApplication).where(LoanApplication.application_ref == application_ref))
            application = app_result.scalar_one_or_none()
            if application is None:
                return json({"error": "Unknown application_ref"}, status=404)
            if application.status != "approved":
                return json({"error": f"Application status is '{application.status}' — risk assessment applies to approved loans"}, status=400)

            service = CollectionsService(db)
            result = await service.assess_risk(application, send_nudge_if_at_risk=body.send_nudge_if_at_risk)

        await log_audit_event(
            action="collections.risk_assess", actor_ref=identity["sub"], actor_role=identity.get("role"),
            target_user_ref=None, details=result,
        )
        return json(result)

    @get("/at-risk")
    async def list_at_risk(self, request: Request) -> json:
        """Portfolio-wide view of approved loans at medium/high repayment
        risk — feeds the admin dashboard's collections widget."""
        identity = require_identity(request)
        if identity.get("role") not in _ANALYST_ROLES:
            return json({"error": "Forbidden: portfolio risk view requires an analyst/admin token."}, status=403)

        async with AsyncSessionLocal() as db:
            service = CollectionsService(db)
            at_risk = await service.list_at_risk_loans(min_risk_level="medium")
            return json({"count": len(at_risk), "loans": at_risk})
