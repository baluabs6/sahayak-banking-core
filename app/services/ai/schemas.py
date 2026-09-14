"""Request validation for the assistant/RAG domain."""
from pydantic import BaseModel, Field


class AssistantAskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    user_ref: str | None = Field(None, max_length=20)
    llm_provider: str | None = Field(None, pattern="^(anthropic|openai|ollama|langchain_auto|gemini|bedrock)$")
    agent_mode: bool = True  # False = skip tool-calling, plain RAG document answer only
