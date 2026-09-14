"""
AI/RAG assistant routes — BlackSheep Router.
Mounted at /api/v1/assistant in app/main.py.
Lets the caller pick any of the 4 supported LLM backends per-request.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
<<<<<<< HEAD
from pydantic import ValidationError

from app.config import get_settings
from app.core.audit import log_audit_event
from app.core.auth_middleware import require_identity
=======
from sqlalchemy import select

from app.config import get_settings
from app.db.postgres import AsyncSessionLocal
>>>>>>> 3646832acdd6b8b99d0b0da0a8bec52147ac1cdf
from app.db.redis_client import check_rate_limit
from app.models.postgres_models import User
from app.services.ai.rag_service import RAGService
from app.services.ai.schemas import AssistantAskRequest

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
<<<<<<< HEAD
        """Body: { "query": "...", "llm_provider": "anthropic|openai|ollama|langchain_auto" (optional) }"""
        # Authenticated (any valid role) — this is a general Q&A surface, not
        # tied to one user's financial records, so no ownership check beyond
        # "you have a valid token" is required here.
        auth_identity = require_identity(request)
=======
        """Body: { "query": "...", "llm_provider": "anthropic|openai|ollama|langchain_auto" (optional),
        "user_ref": "USR-1005" (optional — unlocks account-specific tools: credit score,
        loan/claim status, fraud-flag explanation; omit for pure document Q&A),
        "agent_mode": true (default) | false (skip tool-calling, plain RAG document answer only) }"""
        payload = await request.json()
        query = payload.get("query")
        if not query:
            return json({"error": "query is required"}, status=400)
>>>>>>> 3646832acdd6b8b99d0b0da0a8bec52147ac1cdf

        raw = await request.json()
        try:
            body = AssistantAskRequest.model_validate(raw)
        except ValidationError as e:
            return json({"error": "Invalid request", "details": e.errors()}, status=400)

        # Rate-limit per caller (token identity takes precedence over a
        # client-supplied user_ref, which could previously be spoofed to
        # dodge the limiter entirely).
        rate_limit_identity = auth_identity.get("sub") or body.user_ref or (request.client_ip or "anonymous")
        allowed, current_count = await check_rate_limit(
            rate_limit_identity, route="assistant_ask", limit=settings.rate_limit_assistant_per_minute, window_seconds=60
        )
        if not allowed:
            return json(
                {"error": "Rate limit exceeded. Try again shortly.", "limit_per_minute": settings.rate_limit_assistant_per_minute},
                status=429,
            )

<<<<<<< HEAD
        result = await _rag_service.answer(body.query, provider_override=body.llm_provider)

        await log_audit_event(
            action="assistant.ask", actor_ref=auth_identity.get("sub"), actor_role=auth_identity.get("role"),
            target_user_ref=body.user_ref, details={"llm_provider": result.get("llm_provider"), "cache_hit": result.get("cache_hit")},
        )
        return json(result)
=======
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
>>>>>>> 3646832acdd6b8b99d0b0da0a8bec52147ac1cdf
