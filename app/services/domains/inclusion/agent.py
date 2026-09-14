"""
Inclusion — Proactive Outreach Agent (Credit-Improvement Coach).

Extends the existing one-off "explain my score" feature
(InclusionService.score_user -> RAGService.explain_credit_decision) into an
ongoing coaching loop: given the transparent scorecard's signal breakdown
(credit_scoring.py's per-factor weights are public/inspectable by design —
see that module's docstring on fair-lending explainability), draft
personalized, actionable steps the user can take to improve their score.

No new tool-calling needed here — the scorecard's signal_breakdown already
contains everything required; this is a targeted prompt over data the
platform already computed, not a new data-gathering agent.
"""
from app.services.ai.llm_provider import get_llm_provider

_COACH_SYSTEM_PROMPT = (
    "You are a credit-improvement coach for an Indian financial inclusion platform "
    "serving users with no formal credit history (gig workers, street vendors, small "
    "farmers). Given a user's alt-data credit score and per-factor signal breakdown, "
    "identify the 1-2 weakest factors and draft specific, actionable, low-jargon steps "
    "they can take (e.g. 'try to pay your electricity/water bills within 3 days of the "
    "due date for the next few months' rather than vague advice like 'improve your "
    "bill payment history'). Be encouraging, not judgmental — many of these users are "
    "being scored for the first time in their lives."
)


class InclusionCoachAgent:
    async def coach(self, score: int, band: str, signal_breakdown: dict[str, float]) -> str:
        provider = get_llm_provider()
        weakest = sorted(signal_breakdown.items(), key=lambda kv: kv[1])[:2]
        user_prompt = (
            f"Score: {score}/900, Band: {band}\n"
            f"Full signal breakdown (0-1 scale, higher is better): {signal_breakdown}\n"
            f"Weakest factors: {weakest}\n"
            "Write 3-4 sentences of specific, actionable coaching."
        )
        return await provider.complete(_COACH_SYSTEM_PROMPT, user_prompt)
