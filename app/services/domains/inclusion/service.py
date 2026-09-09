"""
Inclusion domain service — orchestrates alt-data credit scoring, persists
to Postgres, and generates a customer-facing explanation via the RAG/LLM
layer (grounded in fair-lending phrasing rules, not free-form).

Caching: scoring the same user against the same alt-data snapshot is
deterministic (scorecard) plus one LLM call (explanation). Since alt-data
signals (UPI regularity, bill payments, etc.) are recomputed on a batch
schedule rather than changing every second, we cache the full result in
Redis keyed on user_ref + a hash of the alt_data payload — this avoids a
redundant LLM call (cost + latency) for repeat requests within the TTL,
e.g. a lending decision and a customer-facing "check my score" call
landing seconds apart with unchanged inputs.
"""
import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.redis_client import cache_get_json, cache_set_json, make_cache_key
from app.models.postgres_models import CreditScore, User
from app.services.ai.rag_service import RAGService
from app.services.domains.inclusion.credit_scoring import compute_score

_MODEL_VERSION = "alt-data-scorecard-v1"
settings = get_settings()


class InclusionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.rag = RAGService()

    async def score_user(self, user: User, alt_data: dict, provider_override: str | None = None) -> dict:
        cache_key = make_cache_key("credit_score", user.user_ref, json.dumps(alt_data, sort_keys=True), provider_override or "default")
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return {**cached, "cache_hit": True}

        result = compute_score(alt_data)

        explanation = await self.rag.explain_credit_decision(
            score=result.score,
            band=result.band,
            signals=result.signal_breakdown,
            provider_override=provider_override,
        )

        credit_score = CreditScore(
            id=str(uuid.uuid4()),
            user_id=user.id,
            score=result.score,
            score_band=result.band,
            model_version=_MODEL_VERSION,
            explanation=explanation,
        )
        self.db.add(credit_score)
        await self.db.commit()

        response = {
            "user_ref": user.user_ref,
            "score": result.score,
            "band": result.band,
            "signal_breakdown": result.signal_breakdown,
            "explanation": explanation,
            "model_version": _MODEL_VERSION,
        }

        await cache_set_json(cache_key, response, ttl_seconds=settings.cache_ttl_credit_score_seconds)

        return {**response, "cache_hit": False}
