"""
AI/RAG assistant routes — BlackSheep Router.
Mounted at /api/v1/assistant in app/main.py.
Lets the caller pick any of the 4 supported LLM backends per-request.
"""
from blacksheep import Request, json
from blacksheep.server.controllers import APIController, post
from pydantic import ValidationError

from app.config import get_settings
from app.core.audit import log_audit_event
from app.core.auth_middleware import require_identity
from app.db.redis_client import check_rate_limit
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
        """Body: { "query": "...", "llm_provider": "anthropic|openai|ollama|langchain_auto" (optional) }"""
        # Authenticated (any valid role) — this is a general Q&A surface, not
        # tied to one user's financial records, so no ownership check beyond
        # "you have a valid token" is required here.
        auth_identity = require_identity(request)

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

        result = await _rag_service.answer(body.query, provider_override=body.llm_provider)

        await log_audit_event(
            action="assistant.ask", actor_ref=auth_identity.get("sub"), actor_role=auth_identity.get("role"),
            target_user_ref=body.user_ref, details={"llm_provider": result.get("llm_provider"), "cache_hit": result.get("cache_hit")},
        )
        return json(result)
