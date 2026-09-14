"""
Provider-agnostic LLM layer.

Design: a single `LLMProvider` interface with four concrete implementations
(Anthropic, OpenAI, Ollama-local, and a LangChain-wrapped adapter that can
swap between any LangChain-supported chat model at runtime via config).
`get_llm_provider()` is the single factory the rest of the app depends on —
domain services never import a vendor SDK directly.
"""
from abc import ABC, abstractmethod

from app.config import get_settings

settings = get_settings()


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        """Return a single text completion."""
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        resp = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content or ""


class OllamaProvider(LLMProvider):
    """Local/open-source model runner via Ollama's HTTP API — no external API key needed."""
    name = "ollama"

    def __init__(self) -> None:
        import httpx

        self._base_url = settings.ollama_base_url
        self._model = settings.ollama_model
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=120)

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        resp = await self._client.post(
            "/api/chat",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


class GeminiProvider(LLMProvider):
    """Google Gemini via the google-generativeai SDK. Kept as a direct
    provider (not only via LangChain) for cost/quota diversification —
    useful as a fallback if Anthropic/OpenAI quota is exhausted."""
    name = "gemini"

    def __init__(self) -> None:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        self._model_name = settings.gemini_model
        self._genai = genai

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        model = self._genai.GenerativeModel(self._model_name, system_instruction=system_prompt)
        # google-generativeai's async client; generation_config caps output length.
        response = await model.generate_content_async(
            user_prompt,
            generation_config={"max_output_tokens": max_tokens},
        )
        return response.text or ""


class BedrockProvider(LLMProvider):
    """AWS Bedrock — routes LLM calls through the same AWS account/VPC/IAM
    boundary as S3/DynamoDB/SNS/CloudWatch, which matters for a banking
    system's data-governance story (no data leaving AWS to a third-party
    API for this call path). Supports any Bedrock-hosted model (Anthropic,
    Meta Llama, Amazon Titan, Mistral) via `settings.bedrock_model_id`."""
    name = "bedrock"

    def __init__(self) -> None:
        import boto3

        self._client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        self._model_id = settings.bedrock_model_id

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        import asyncio
        import json as _json

        def _invoke() -> str:
            # Anthropic-on-Bedrock message format; adjust the body shape if
            # settings.bedrock_model_id points at a non-Anthropic model.
            body = _json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
            )
            resp = self._client.invoke_model(modelId=self._model_id, body=body)
            payload = _json.loads(resp["body"].read())
            return "".join(block.get("text", "") for block in payload.get("content", []))

        # boto3 is sync; run off the event loop so it doesn't block other requests.
        return await asyncio.to_thread(_invoke)


class LangChainProvider(LLMProvider):
    """Provider-agnostic adapter using LangChain's chat-model abstraction.
    Lets you swap the underlying model (Anthropic/OpenAI/Ollama/others)
    purely via LangChain config, useful when a domain service wants to
    stay 100% vendor-neutral and rely on LangChain's own routing instead."""
    name = "langchain_auto"

    def __init__(self) -> None:
        self._model = self._build_chat_model()

    def _build_chat_model(self):
        # Chooses the underlying LangChain chat model based on which
        # credentials are configured — first match wins.
        if settings.anthropic_api_key:
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(model=settings.anthropic_model, api_key=settings.anthropic_api_key)
        if settings.openai_api_key:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key)
        from langchain_community.chat_models import ChatOllama

        return ChatOllama(model=settings.ollama_model, base_url=settings.ollama_base_url)

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        result = await self._model.ainvoke(messages)
        return result.content


_PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "bedrock": BedrockProvider,
    "langchain_auto": LangChainProvider,
}


def get_llm_provider(provider_override: str | None = None) -> LLMProvider:
    """Factory used by every domain service. Reads `LLM_PROVIDER` from config
    by default; callers may override per-call (e.g. force Ollama for
    on-prem/offline inference in a bank branch with no internet)."""
    provider_key = provider_override or settings.llm_provider
    provider_cls = _PROVIDERS.get(provider_key)
    if provider_cls is None:
        raise ValueError(f"Unknown LLM provider '{provider_key}'. Options: {list(_PROVIDERS)}")
    return provider_cls()
