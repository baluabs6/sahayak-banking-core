"""
Append-only-style audit logging for security-relevant / financial actions.

Not implemented previously: nothing recorded *who* computed a credit score,
evaluated a transaction, changed a loan decision, or filed a claim. That's
both a forensics gap and, for lending/insurance decisions, a compliance gap
(fair-lending / dispute-resolution processes need to reconstruct "what did
we know and decide, and who/what triggered it").

Design: written to Mongo (`audit_logs`) since it's high-write and
schema-light, same rationale as fraud_events. Never raises - an audit-log
failure must not block the underlying business action, but it does log a
local error so an audit-pipeline outage itself is visible in CloudWatch.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from app.db.mongo import COLLECTION_AUDIT_LOGS, get_mongo_db

logger = logging.getLogger("audit")


async def log_audit_event(
    action: str,
    actor_ref: str | None,
    actor_role: str | None,
    target_user_ref: str | None,
    details: dict[str, Any] | None = None,
    outcome: str = "success",
) -> None:
    """
    action: short machine-readable event name, e.g. "credit_score.compute",
            "fraud.transaction_evaluate", "loan.decision", "claim.file",
            "auth.token_issue", "auth.token_denied".
    actor_ref / actor_role: who performed the action (from the verified JWT).
    target_user_ref: whose record was affected (may differ from actor for
            analyst/admin actions - that divergence is exactly what audit
            logs need to capture).
    """
    event = {
        "action": action,
        "actor_ref": actor_ref,
        "actor_role": actor_role,
        "target_user_ref": target_user_ref,
        "outcome": outcome,
        "details": details or {},
        "created_at": datetime.now(timezone.utc),
    }
    try:
        db = get_mongo_db()
        await db[COLLECTION_AUDIT_LOGS].insert_one(event)
    except Exception:
        logger.exception("audit_log_write_failed", extra={"action": action, "actor_ref": actor_ref})
