"""
AI/RAG assistant routes — BlackSheep Router.
Mounted at /api/v1/assistant in app/main.py.
Lets the caller pick any of the 4 supported LLM backends per-request.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
from sqlalchemy import select

from app.config import get_settings
from app.db.postgres import AsyncSessionLocal
from app.db.redis_client import check_rate_limit
from app.models.postgres_models import User
from app.services.ai.rag_service import RAGService

_rag_service = RAGService()
settings = get_settings()

# Small illustrative knowledge base — in production, sourced from RBI
# circulars, scheme docs, and product T&Cs, refreshed by a batch job.
_SAMPLE_KNOWLEDGE_BASE = [
    {
        "id": "kb-1",
        "source": "RBI_priority_sector_lending_guidelines",
        "text": (
            "Priority sector lending in India includes agriculture, micro and small "
            "enterprises, and weaker sections. Banks must meet an overall PSL target "
            "as a percentage of adjusted net bank credit."
        ),
    },
    {
        "id": "kb-2",
        "source": "PMFBY_crop_insurance_scheme",
        "text": (
            "The Pradhan Mantri Fasal Bima Yojana provides crop insurance with premium "
            "shared between farmer and government, using weather and yield-based triggers "
            "for faster claim settlement in eligible districts."
        ),
    },
    {
        "id": "kb-3",
        "source": "RBI_fair_lending_practices",
        "text": (
            "Lenders using automated credit scoring must be able to provide a customer "
            "with a clear, non-technical explanation of the factors behind a credit decision."
        ),
    },
]
_rag_service.build_index(_SAMPLE_KNOWLEDGE_BASE)


class AssistantController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/assistant"

    @post("/ask")
    async def ask(self, request: Request) -> json:
        """Body: { "query": "...", "llm_provider": "anthropic|openai|ollama|langchain_auto" (optional),
        "user_ref": "USR-1005" (optional — unlocks account-specific tools: credit score,
        loan/claim status, fraud-flag explanation; omit for pure document Q&A),
        "agent_mode": true (default) | false (skip tool-calling, plain RAG document answer only) }"""
        payload = await request.json()
        query = payload.get("query")
        if not query:
            return json({"error": "query is required"}, status=400)

        # Rate-limit per caller (identified by user_ref if provided, else client IP)
        # to cap paid-LLM spend and abuse — Redis-backed fixed window.
        identity = payload.get("user_ref") or (request.client_ip or "anonymous")
        allowed, current_count = await check_rate_limit(
            identity, route="assistant_ask", limit=settings.rate_limit_assistant_per_minute, window_seconds=60
        )
        if not allowed:
            return json(
                {"error": "Rate limit exceeded. Try again shortly.", "limit_per_minute": settings.rate_limit_assistant_per_minute},
                status=429,
            )

        if not payload.get("agent_mode", True):
            # Cheap path: a single retrieval + single LLM call, no tool-calling
            # loop — useful when the caller just wants the old pure-RAG
            # behavior without the extra LLM round-trips a tool-calling agent
            # can take (cost/latency control, per the cross-cutting guardrail).
            result = await _rag_service.answer(query, provider_override=payload.get("llm_provider"))
            return json(result)

        # Multi-tool agent: routes to domain tools (credit score, loan/claim
        # status, fraud-flag explanation) instead of only static documents —
        # this is the default because the account-specific tools only ever
        # expose the calling user's own data (see assistant_agent.py), so
        # there's no new access-control surface being opened by defaulting
        # to it.
        from app.services.ai.assistant_agent import MultiToolAssistant

        user_ref = payload.get("user_ref")
        async with AsyncSessionLocal() as db:
            user = None
            if user_ref:
                user_result = await db.execute(select(User).where(User.user_ref == user_ref))
                user = user_result.scalar_one_or_none()
                if user is None:
                    return json({"error": "Unknown user_ref"}, status=404)

            assistant = MultiToolAssistant(db, _rag_service)
            result = await assistant.ask(query, user)
            return json(result)
