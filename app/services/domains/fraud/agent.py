"""
Fraud — Analyst Copilot Agent.

Fraud detection itself (rules + Isolation Forest + narrative) is handled by
anomaly_detection.py/service.py. This module is the *agentic action* layer
on top: given a flagged transaction, an agent autonomously queries the same
signals a human analyst would (DynamoDB velocity, Mongo event history, user
profile), then RECOMMENDS an action — freeze card, step-up auth, or clear.

Human-in-the-loop guardrail: the agent's tools are all read-only lookups
plus a single `propose_action` tool. It never calls a tool that actually
freezes an account or blocks a card — "propose then confirm" per the
cross-cutting guardrail in the brief. A human analyst (or a separate,
explicitly-authorized confirm endpoint) executes the action; this module
only recommends and logs its reasoning trail to Mongo for audit.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import Transaction, User
from app.services.aws.dynamodb_service import DynamoDBService
from app.services.ai.agent_orchestrator import AgentOrchestrator, Tool, log_agent_run

_INVESTIGATOR_SYSTEM_PROMPT = (
    "You are a fraud analyst copilot for an Indian bank. You've been handed a "
    "flagged transaction. Use the tools available to look up the user's recent "
    "transaction velocity, event history, and profile before forming an opinion. "
    "When you've gathered enough context, call `propose_action` exactly once with "
    "your recommended action ('freeze_card', 'step_up_auth', or 'clear') and a "
    "short justification. You are recommending only — a human analyst confirms "
    "and executes the action; do not claim the action has already been taken."
)


class FraudInvestigatorAgent:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.dynamodb = DynamoDBService()

    def _build_tools(self, user: User) -> list[Tool]:
        async def get_velocity_counters(_input: dict) -> dict:
            window_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            try:
                count = self.dynamodb.get_txn_velocity(user.id, window_key)
            except Exception:
                count = None
            return {"txns_in_current_minute_window": count}

        async def get_fraud_event_history(_input: dict) -> dict:
            from app.db.mongo import COLLECTION_FRAUD_EVENTS, get_mongo_db

            db_mongo = get_mongo_db()
            cursor = db_mongo[COLLECTION_FRAUD_EVENTS].find({"user_id": user.id}).sort("created_at", -1).limit(10)
            events = [
                {
                    "txn_ref": e.get("txn_ref"),
                    "rule_triggers": e.get("rule_triggers"),
                    "ml_anomaly_score": e.get("ml_anomaly_score"),
                    "action_taken": e.get("action_taken"),
                    "created_at": str(e.get("created_at")),
                }
                async for e in cursor
            ]
            return {"recent_fraud_events": events}

        async def get_user_profile(_input: dict) -> dict:
            return {
                "user_ref": user.user_ref,
                "region": user.region,
                "state": user.state,
                "kyc_status": user.kyc_status,
                "has_formal_credit_history": user.has_formal_credit_history,
                "aadhaar_linked": user.aadhaar_linked,
            }

        async def get_recent_transactions(_input: dict) -> dict:
            result = await self.db.execute(
                select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.timestamp.desc()).limit(10)
            )
            txns = result.scalars().all()
            return {
                "recent_transactions": [
                    {
                        "txn_ref": t.txn_ref,
                        "amount_inr": float(t.amount_inr),
                        "txn_type": t.txn_type,
                        "location": t.location,
                        "device_id": t.device_id,
                        "is_flagged": t.is_flagged,
                    }
                    for t in txns
                ]
            }

        async def propose_action(action_input: dict) -> dict:
            # Deliberately a no-op beyond echoing the proposal back — this is
            # the human-in-the-loop boundary. See module docstring.
            return {
                "status": "proposed_pending_human_confirmation",
                "action": action_input.get("action"),
                "justification": action_input.get("justification"),
            }

        return [
            Tool(
                name="get_velocity_counters",
                description="Fetch the user's current per-minute transaction velocity from DynamoDB.",
                input_schema={"type": "object", "properties": {}},
                handler=get_velocity_counters,
            ),
            Tool(
                name="get_fraud_event_history",
                description="Fetch the user's recent fraud events (rule triggers, ML scores, actions taken) from MongoDB.",
                input_schema={"type": "object", "properties": {}},
                handler=get_fraud_event_history,
            ),
            Tool(
                name="get_user_profile",
                description="Fetch the user's KYC/profile summary.",
                input_schema={"type": "object", "properties": {}},
                handler=get_user_profile,
            ),
            Tool(
                name="get_recent_transactions",
                description="Fetch the user's 10 most recent transactions.",
                input_schema={"type": "object", "properties": {}},
                handler=get_recent_transactions,
            ),
            Tool(
                name="propose_action",
                description=(
                    "Propose a recommended action for a human analyst to confirm. Call this exactly once, "
                    "after gathering enough context. Does NOT execute the action itself."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["freeze_card", "step_up_auth", "clear"]},
                        "justification": {"type": "string"},
                    },
                    "required": ["action", "justification"],
                },
                handler=propose_action,
            ),
        ]

    async def investigate(self, user: User, flagged_transaction: dict) -> dict:
        """Given a flagged transaction (as returned by FraudService.evaluate_transaction),
        runs the investigator agent and returns its recommendation + full trace,
        already logged to Mongo for audit."""
        tools = self._build_tools(user)
        orchestrator = AgentOrchestrator(tools=tools, system_prompt=_INVESTIGATOR_SYSTEM_PROMPT)

        user_prompt = (
            f"Flagged transaction: {flagged_transaction}\n"
            f"User: {user.user_ref}\n"
            "Investigate and propose an action."
        )
        result = await orchestrator.run(user_prompt)
        await log_agent_run("fraud_investigator_agent", user.id, result, extra={"flagged_transaction": flagged_transaction})

        return {
            "summary": result.final_text,
            "requires_confirmation": result.requires_confirmation,
            "proposed_action": result.proposed_action,
            "tool_calls": [{"tool": t.tool_name, "input": t.tool_input} for t in result.trace],
        }

    async def draft_case_file(self, user: User, flagged_transaction: dict) -> str:
        """SNS-triggered agent loop, lightweight version: instead of just
        alerting an analyst with raw logs, pre-drafts a case-file summary so
        the human reviewer starts from a synthesis, not raw event dumps.
        Reuses the same tools/context-gathering as investigate(), but asks
        for a case-file writeup instead of an action proposal."""
        from app.services.ai.llm_provider import get_llm_provider

        tools = self._build_tools(user)
        # Gather context via the same read-only tools, without needing the model —
        # call the handlers directly for a fast, deterministic case-file draft.
        context = {}
        for tool in tools:
            if tool.name == "propose_action":
                continue
            context[tool.name] = await tool.handler({})

        provider = get_llm_provider()
        system = (
            "You draft concise fraud case-file summaries for a bank's risk analysts. "
            "Given a flagged transaction and supporting context, write a short "
            "structured summary: what happened, why it was flagged, and what the "
            "analyst should check first. Do not recommend an action — that's the "
            "analyst's call."
        )
        user_prompt = f"Flagged transaction: {flagged_transaction}\nContext: {context}"
        return await provider.complete(system, user_prompt)
