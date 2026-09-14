"""
Admin/analyst domain routes — BlackSheep Router.
Mounted at /api/v1/admin in app/main.py.

Every route here requires an analyst/admin token — there is no
customer-facing use of this controller.
"""
from blacksheep import Request, Response, json
from blacksheep.server.controllers import APIController, get, post

from app.core.audit import log_audit_event
from app.core.auth_middleware import require_identity
from app.db.postgres import AsyncSessionLocal
from app.services.domains.admin.service import AdminService

_ANALYST_ROLES = ("analyst", "admin")


def _forbid_unless_analyst(identity: dict) -> Response | None:
    if identity.get("role") not in _ANALYST_ROLES:
        return json({"error": "Forbidden: this endpoint requires an analyst/admin token."}, status=403)
    return None


class AdminController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/admin"

    @get("/dashboard")
    async def dashboard(self, request: Request) -> json:
        identity = require_identity(request)
        deny = _forbid_unless_analyst(identity)
        if deny:
            return deny

        async with AsyncSessionLocal() as db:
            service = AdminService(db)
            summary = await service.dashboard_summary()
            return json(summary)

    @get("/agent-actions/pending")
    async def pending_agent_actions(self, request: Request) -> json:
        """Every agent run that proposed a consequential action (freeze a
        card, approve a loan, trigger a payout) and is still awaiting a
        human reviewer's decision."""
        identity = require_identity(request)
        deny = _forbid_unless_analyst(identity)
        if deny:
            return deny

        async with AsyncSessionLocal() as db:
            service = AdminService(db)
            actions = await service.list_pending_agent_actions()
            return json({"count": len(actions), "pending_actions": actions})

    @post("/agent-actions/{action_id}/confirm")
    async def confirm_agent_action(self, request: Request, action_id: str) -> json:
        """Body: { "approve": true }. Records the human reviewer's decision
        on a proposed agent action. This does NOT itself execute the
        underlying action (freeze the card, approve the loan) — it records
        the reviewer's sign-off; the actual state change still happens
        through the normal domain route (e.g. POST /lending/applications),
        by design, so there is always a second, independently-audited
        write for anything consequential."""
        identity = require_identity(request)
        deny = _forbid_unless_analyst(identity)
        if deny:
            return deny

        raw = {}
        try:
            raw = await request.json()
        except Exception:
            raw = {}
        approve = bool(raw.get("approve", False))

        async with AsyncSessionLocal() as db:
            service = AdminService(db)
            result = await service.confirm_agent_action(action_id, confirmed_by=identity["sub"], approve=approve)
            if "error" in result:
                return json(result, status=400)

        await log_audit_event(
            action="agent_action.confirm", actor_ref=identity["sub"], actor_role=identity.get("role"),
            target_user_ref=None, details=result,
        )
        return json(result)

    @get("/audit-log")
    async def audit_log(self, request: Request) -> json:
        """Query params (both optional): ?user_ref=USR-1001&action=loan.decision
        Read-only view over the append-only audit log written by every
        security/finance-relevant action across every domain."""
        identity = require_identity(request)
        deny = _forbid_unless_analyst(identity)
        if deny:
            return deny

        def _first(value):
            return value[0] if isinstance(value, list) else value

        user_ref = _first(request.query.get("user_ref"))
        action = _first(request.query.get("action"))

        async with AsyncSessionLocal() as db:
            service = AdminService(db)
            events = await service.query_audit_log(target_user_ref=user_ref, action=action)
            return json({"count": len(events), "events": events})
