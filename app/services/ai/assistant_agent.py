"""
Assistant / RAG layer — the multi-tool customer-facing agent.

Upgrades /api/v1/assistant/ask from pure document Q&A into an agent that can
also check the CALLING user's own credit score, loan status, and claim
status, and explain a fraud flag — routing to the domain services via tool
calls instead of only answering from static RBI/scheme documents.

Scope/safety: every tool here is scoped to the single `user_ref` the caller
authenticated as (passed as `user_ref` in the request body, same as every
other route in this codebase) — there is no tool that can look up another
user's data. If no `user_ref` is given, only the document-search tool is
available, so anonymous/unauthenticated queries behave exactly like the
original pure-RAG endpoint.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postgres_models import CreditScore, InsuranceClaim, LoanApplication, Transaction, User
from app.services.ai.agent_orchestrator import AgentOrchestrator, Tool, log_agent_run
from app.services.ai.rag_service import RAGService

_ASSISTANT_SYSTEM_PROMPT = (
    "You are Sahayak, a customer-facing assistant for an Indian financial inclusion "
    "platform (lending, insurance, fraud protection, alt-data credit scoring). Answer "
    "using the tools available — search policy/scheme documents for general questions, "
    "and the account-specific tools for questions about the user's own score, loans, "
    "claims, or a fraud flag. Never invent account details; if a tool returns no data, "
    "say so plainly. Keep answers short, plain-language, and specific to India's "
    "regulatory/product context (RBI, PMFBY, UPI, etc.) where relevant."
)


class MultiToolAssistant:
    def __init__(self, db: AsyncSession, rag: RAGService) -> None:
        self.db = db
        self.rag = rag

    def _build_tools(self, user: User | None) -> list[Tool]:
        async def search_policy_documents(tool_input: dict) -> dict:
            result = await self.rag.answer(tool_input["question"])
            return {"answer": result["answer"], "sources": result["sources"]}

        tools = [
            Tool(
                name="search_policy_documents",
                description="Search RBI circulars, scheme documents (e.g. PMFBY), and product T&Cs for a general policy question.",
                input_schema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
                handler=search_policy_documents,
            )
        ]

        if user is None:
            return tools  # anonymous caller: document search only

        async def check_my_credit_score(_input: dict) -> dict:
            result = await self.db.execute(
                select(CreditScore).where(CreditScore.user_id == user.id).order_by(CreditScore.computed_at.desc()).limit(1)
            )
            score = result.scalar_one_or_none()
            if score is None:
                return {"has_score": False}
            return {"has_score": True, "score": score.score, "band": score.score_band, "explanation": score.explanation}

        async def check_my_loan_status(_input: dict) -> dict:
            result = await self.db.execute(
                select(LoanApplication).where(LoanApplication.user_id == user.id).order_by(LoanApplication.submitted_at.desc()).limit(5)
            )
            apps = result.scalars().all()
            return {
                "applications": [
                    {"application_ref": a.application_ref, "status": a.status, "requested_amount_inr": float(a.requested_amount_inr)}
                    for a in apps
                ]
            }

        async def check_my_claim_status(_input: dict) -> dict:
            result = await self.db.execute(
                select(InsuranceClaim).where(InsuranceClaim.user_id == user.id).order_by(InsuranceClaim.filed_at.desc()).limit(5)
            )
            claims = result.scalars().all()
            return {
                "claims": [
                    {"claim_ref": c.claim_ref, "status": c.status, "auto_triggered": c.auto_triggered, "claim_amount_inr": float(c.claim_amount_inr)}
                    for c in claims
                ]
            }

        async def explain_my_fraud_flag(tool_input: dict) -> dict:
            txn_ref = tool_input.get("txn_ref")
            query = select(Transaction).where(Transaction.user_id == user.id, Transaction.is_flagged.is_(True))
            if txn_ref:
                query = query.where(Transaction.txn_ref == txn_ref)
            result = await self.db.execute(query.order_by(Transaction.timestamp.desc()).limit(1))
            txn = result.scalar_one_or_none()
            if txn is None:
                return {"found": False}
            return {
                "found": True,
                "txn_ref": txn.txn_ref,
                "amount_inr": float(txn.amount_inr),
                "txn_type": txn.txn_type,
                "fraud_score": txn.fraud_score,
                "timestamp": txn.timestamp.isoformat(),
            }

        tools += [
            Tool(
                name="check_my_credit_score",
                description="Fetch the calling user's own latest alt-data credit score.",
                input_schema={"type": "object", "properties": {}},
                handler=check_my_credit_score,
            ),
            Tool(
                name="check_my_loan_status",
                description="Fetch the calling user's own recent loan applications and their status.",
                input_schema={"type": "object", "properties": {}},
                handler=check_my_loan_status,
            ),
            Tool(
                name="check_my_claim_status",
                description="Fetch the calling user's own recent insurance claims and their status.",
                input_schema={"type": "object", "properties": {}},
                handler=check_my_claim_status,
            ),
            Tool(
                name="explain_my_fraud_flag",
                description="Fetch details of the calling user's most recent flagged transaction (optionally by txn_ref), to help explain why it was flagged.",
                input_schema={"type": "object", "properties": {"txn_ref": {"type": "string"}}},
                handler=explain_my_fraud_flag,
            ),
        ]
        return tools

    async def ask(self, query: str, user: User | None) -> dict:
        tools = self._build_tools(user)
        orchestrator = AgentOrchestrator(tools=tools, system_prompt=_ASSISTANT_SYSTEM_PROMPT)
        result = await orchestrator.run(query)
        if user is not None:
            await log_agent_run("multi_tool_assistant", user.id, result, extra={"query": query})

        # Surface document sources at the top level for backward compatibility
        # with the previous pure-RAG /ask response shape, which always
        # returned a top-level "sources" list.
        sources: list = []
        for record in result.trace:
            if record.tool_name == "search_policy_documents" and isinstance(record.tool_result, dict):
                sources.extend(record.tool_result.get("sources", []))

        return {
            "answer": result.final_text,
            "sources": sources,
            "tool_calls": [{"tool": t.tool_name, "input": t.tool_input} for t in result.trace],
        }
