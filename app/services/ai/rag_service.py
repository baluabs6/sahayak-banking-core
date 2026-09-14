"""
RAG (Retrieval-Augmented Generation) service.

Use cases in this app:
  1. Customer support / financial-literacy Q&A grounded in RBI guidelines,
     product T&Cs, and scheme eligibility documents (in the local language).
  2. Generating a plain-language explanation for *why* a credit score or
     fraud flag was assigned — grounded in the actual policy documents,
     not a free-form hallucination.

Vector store: FAISS locally for dev; swap to pgvector-on-Postgres in
production so we don't operate a third datastore.
"""
from app.config import get_settings
from app.db.redis_client import cache_get_json, cache_set_json, make_cache_key
from app.services.ai.llm_provider import get_llm_provider

settings = get_settings()

_RAG_SYSTEM_PROMPT = (
    "You are a banking assistant for an Indian financial inclusion platform. "
    "Answer ONLY using the provided context documents. If the context doesn't "
    "contain the answer, say you don't have that information and suggest the "
    "user contact a branch. Be concise, avoid jargon, and never invent policy "
    "details, interest rates, or eligibility rules."
)


class RAGService:
    def __init__(self) -> None:
        self._vector_store = None  # lazily built
        self._bm25_retriever = None  # lazily built, only used when hybrid retrieval is enabled
        self._documents: list[dict] = []

    def _get_embeddings(self):
        if settings.embedding_provider == "anthropic":
            # Anthropic does not currently expose a public embeddings endpoint;
            # falls back to a local sentence-transformer model for embeddings
            # while generation still runs on Claude via llm_provider.
            from langchain_community.embeddings import HuggingFaceEmbeddings

            return HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        if settings.embedding_provider == "openai":
            from langchain_openai import OpenAIEmbeddings

            return OpenAIEmbeddings(api_key=settings.openai_api_key)
        from langchain_community.embeddings import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

    def build_index(self, documents: list[dict]) -> None:
        """documents: [{"id": ..., "text": ..., "source": ...}, ...]
        In production this runs as a batch job whenever RBI circulars,
        product T&Cs, or scheme documents change — not per-request."""
        from langchain_community.vectorstores import FAISS
        from langchain_core.documents import Document

        docs = [Document(page_content=d["text"], metadata={"id": d["id"], "source": d["source"]}) for d in documents]
        self._vector_store = FAISS.from_documents(docs, self._get_embeddings())
        self._documents = documents

        if settings.use_hybrid_retrieval:
            # Sparse (keyword) retriever alongside the dense FAISS one.
            # Matters for RBI-circular/scheme text, where exact terms (scheme
            # names, section numbers) are often a better match signal than
            # embedding similarity alone catches.
            from langchain_community.retrievers import BM25Retriever

            self._bm25_retriever = BM25Retriever.from_documents(docs)

    def _retrieve(self, query: str, k: int = 4) -> list[dict]:
        if self._vector_store is None:
            return []

        if settings.use_hybrid_retrieval and self._bm25_retriever is not None:
            from langchain.retrievers import EnsembleRetriever

            self._bm25_retriever.k = k
            dense_retriever = self._vector_store.as_retriever(search_kwargs={"k": k})
            ensemble = EnsembleRetriever(
                retrievers=[dense_retriever, self._bm25_retriever],
                weights=[settings.hybrid_dense_weight, 1 - settings.hybrid_dense_weight],
            )
            results = ensemble.invoke(query)[:k]
        else:
            results = self._vector_store.similarity_search(query, k=k)

        return [{"id": r.metadata.get("id"), "source": r.metadata.get("source"), "text": r.page_content} for r in results]

    async def answer(self, query: str, provider_override: str | None = None) -> dict:
        # Cache keyed on the normalized query + provider: identical questions
        # (common for FAQ-style scheme/policy queries) skip a paid LLM call
        # entirely within the TTL window.
        cache_key = make_cache_key("rag_answer", query.strip().lower(), provider_override or settings.llm_provider)
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return {**cached, "cache_hit": True}

        retrieved = self._retrieve(query)
        context = "\n\n".join(f"[{d['source']}] {d['text']}" for d in retrieved) or "No matching documents found."

        provider = get_llm_provider(provider_override)
        user_prompt = f"Context:\n{context}\n\nQuestion: {query}"
        answer_text = await provider.complete(_RAG_SYSTEM_PROMPT, user_prompt)

        response = {
            "answer": answer_text,
            "sources": [d["source"] for d in retrieved],
            "llm_provider": provider.name,
        }
        await cache_set_json(cache_key, response, ttl_seconds=settings.cache_ttl_rag_answer_seconds)

        return {**response, "cache_hit": False}

    async def explain_credit_decision(self, score: int, band: str, signals: dict, provider_override: str | None = None) -> str:
        """Generates a grounded, plain-language explanation of a credit score
        for the end user — required for lending transparency/fair-lending
        compliance, not just a nice-to-have."""
        provider = get_llm_provider(provider_override)
        system = (
            "You explain automated credit scoring decisions to Indian retail/MSME "
            "customers in plain, respectful language. Do not use technical ML jargon. "
            "Mention 2-3 concrete factors that helped or hurt the score."
        )
        user_prompt = (
            f"Score: {score}/900, Band: {band}\n"
            f"Underlying signals (JSON): {signals}\n"
            "Write a 3-4 sentence explanation for the customer."
        )
        return await provider.complete(system, user_prompt)
