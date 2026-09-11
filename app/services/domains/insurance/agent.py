"""
Insurance — Claims Agent.

InsuranceService (service.py) already auto-triggers payout for parametric
claims (index crosses a threshold). This module handles the other case:
non-parametric claims that need manual assessment. An agent reads the claim
narrative + KYC docs and drafts a recommended settlement citing policy
terms via the RAG layer — plus a lightweight anomalous-claim check against
the fraud service before any payout recommendation goes out.

Human-in-the-loop guardrail: like the lending/fraud agents, this drafts a
RECOMMENDATION for a human claims assessor. It never marks a claim as paid.
"""
from app.services.ai.agent_orchestrator import log_agent_run, AgentRunResult
from app.services.ai.llm_provider import get_llm_provider
from app.services.ai.rag_service import RAGService


class InsuranceClaimsAgent:
    def __init__(self) -> None:
        self.rag = RAGService()

    async def triage_claim(self, user_id: str, claim: dict, kyc_documents: list[dict] | None = None) -> dict:
        """Non-parametric claims triage: reads the claim narrative (+ any KYC
        doc metadata passed in) and drafts a recommended settlement, grounded
        in policy/scheme documents via the RAG layer rather than free-form
        invention — reusing the same "answer only from retrieved context"
        RAG pattern already used for RBI/scheme Q&A."""
        query = (
            f"What are the settlement guidelines for a {claim.get('policy_type')} claim "
            f"with trigger event '{claim.get('trigger_event')}' in {claim.get('region')}?"
        )
        rag_result = await self.rag.answer(query)

        provider = get_llm_provider()
        system = (
            "You are a claims triage assistant for an Indian insurance platform. Given "
            "a claim's details, any KYC document metadata, and grounded policy context, "
            "draft a recommended settlement decision (approve_full, approve_partial, "
            "deny, or request_more_info) with a short justification citing the policy "
            "context given. This is a recommendation for a human claims assessor — do "
            "not state that the claim has been settled."
        )
        user_prompt = (
            f"Claim: {claim}\n"
            f"KYC document metadata: {kyc_documents or 'none provided'}\n"
            f"Policy context: {rag_result['answer']}\n"
            "Draft your recommendation."
        )
        recommendation = await provider.complete(system, user_prompt)

        result = AgentRunResult(final_text=recommendation, trace=[])
        await log_agent_run("insurance_claims_triage_agent", user_id, result, extra={"claim": claim, "policy_sources": rag_result["sources"]})

        return {
            "recommendation": recommendation,
            "policy_sources": rag_result["sources"],
        }

    async def flag_anomalous_claim(self, user_id: str, claim: dict, recent_claims_for_user: list[dict]) -> dict:
        """Anomalous-claim flagging: a lightweight cross-check (claim
        frequency + amount pattern) modeled on the fraud service's rule
        style, deciding whether to route a claim to fraud review before any
        payout — without duplicating the full ML fraud engine, since claims
        volume/shape differs enough from transaction data to warrant its own
        simple rules rather than reusing the transaction Isolation Forest."""
        claim_amount = float(claim.get("claim_amount_inr", 0))
        same_type_recent = [c for c in recent_claims_for_user if c.get("policy_type") == claim.get("policy_type")]

        triggers = []
        if len(same_type_recent) >= 3:
            triggers.append("repeated_claims_same_policy_type")
        if claim_amount >= 100_000:
            triggers.append("high_value_claim")
        if any(c.get("auto_triggered") is False and c.get("status") == "paid" for c in same_type_recent):
            triggers.append("prior_manually_approved_claim_same_type")

        route_to_fraud_review = len(triggers) >= 2  # any single signal alone is common and not inherently suspicious

        result = AgentRunResult(
            final_text=f"route_to_fraud_review={route_to_fraud_review}, triggers={triggers}",
            trace=[],
        )
        await log_agent_run("insurance_anomalous_claim_check", user_id, result, extra={"claim": claim, "triggers": triggers})

        return {"route_to_fraud_review": route_to_fraud_review, "triggers": triggers}
