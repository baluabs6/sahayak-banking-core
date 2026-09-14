"""
Agent orchestration layer — the shared tool-calling harness every domain
agent (lending underwriter, fraud investigator, insurance triage, inclusion
coach) is built on, plus the multi-tool assistant orchestrator.

Design notes / scope:
  - Tool-calling requires a model API that supports structured tool use.
    `app/services/ai/llm_provider.py`'s `LLMProvider.complete()` is a plain
    text-in/text-out interface across all 4 backends (Anthropic/OpenAI/
    Ollama/LangChain) — deliberately, so ordinary narrative generation
    (fraud narratives, credit explanations) stays provider-agnostic. Agent
    tool-calling is a different, richer contract, so this module talks to
    the Anthropic Messages API directly rather than stretching the plain
    `complete()` interface to cover it. If OpenAI/Ollama tool-calling
    support is needed later, add a `complete_with_tools` method to
    `LLMProvider` with per-provider implementations and swap the client
    construction below for `get_llm_provider()`.
  - Every agent built on this orchestrator follows a propose-then-confirm
    pattern for anything that would take a real action (freeze a card,
    approve a loan, trigger a payout) — see AgentRunResult.requires_confirmation.
    The agent's tool set for action-taking tools should register a
    "propose_*" tool, not a tool that executes directly; the caller
    (a human reviewer, in the banking flows here) confirms separately.
  - Every run's full tool-call trace is designed to be logged to Mongo
    (COLLECTION_AGENT_AUDIT_LOG in app/db/mongo.py) for audit purposes —
    callers should persist `AgentRunResult.trace` after every run.
"""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import get_settings

settings = get_settings()

ToolHandler = Callable[[dict], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict  # JSON schema for the tool's input, per Anthropic's tool-use spec
    handler: ToolHandler


@dataclass
class ToolCallRecord:
    tool_name: str
    tool_input: dict
    tool_result: Any


@dataclass
class AgentRunResult:
    final_text: str
    trace: list[ToolCallRecord] = field(default_factory=list)
    requires_confirmation: bool = False
    proposed_action: dict | None = None
    turns_used: int = 0


class AgentOrchestrator:
    """A minimal, explicit agent loop: send messages + tool definitions to
    Claude, execute any tool the model calls, feed results back, repeat
    until the model stops calling tools or `max_turns` is hit.

    Deliberately NOT a general-purpose framework — banking decisions need an
    auditable, inspectable trace of every tool call and its result, so the
    loop is kept small and easy to reason about rather than hidden behind a
    library abstraction.
    """

    def __init__(self, tools: list[Tool], system_prompt: str, max_turns: int = 6) -> None:
        self._tools = {t.name: t for t in tools}
        self._system_prompt = system_prompt
        self._max_turns = max_turns

    def _tool_definitions(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self._tools.values()
        ]

    async def run(self, user_prompt: str) -> AgentRunResult:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        messages: list[dict] = [{"role": "user", "content": user_prompt}]
        trace: list[ToolCallRecord] = []
        requires_confirmation = False
        proposed_action: dict | None = None

        for turn in range(1, self._max_turns + 1):
            response = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=1500,
                system=self._system_prompt,
                tools=self._tool_definitions(),
                messages=messages,
            )

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b.text for b in response.content if b.type == "text"]
            final_text = "\n".join(text_blocks)

            if not tool_use_blocks:
                return AgentRunResult(
                    final_text=final_text,
                    trace=trace,
                    requires_confirmation=requires_confirmation,
                    proposed_action=proposed_action,
                    turns_used=turn,
                )

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in tool_use_blocks:
                tool = self._tools.get(block.name)
                if tool is None:
                    result: Any = {"error": f"Unknown tool '{block.name}'"}
                else:
                    result = await tool.handler(block.input)
                    if block.name.startswith("propose_"):
                        requires_confirmation = True
                        proposed_action = {"tool": block.name, "input": block.input, "result": result}
                trace.append(ToolCallRecord(tool_name=block.name, tool_input=block.input, tool_result=result))
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": _stringify(result)}
                )
            messages.append({"role": "user", "content": tool_results})

        return AgentRunResult(
            final_text="Agent reached the turn limit before reaching a final answer.",
            trace=trace,
            requires_confirmation=requires_confirmation,
            proposed_action=proposed_action,
            turns_used=self._max_turns,
        )


def _stringify(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


async def log_agent_run(agent_name: str, user_id: str, result: AgentRunResult, extra: dict | None = None) -> None:
    """Persists an agent run's full tool-call trace to Mongo for audit —
    required for any agent touching a lending/fraud/insurance decision.
    Best-effort: an audit-logging failure should never fail the request that
    already completed, so this deliberately never raises."""
    from datetime import datetime, timezone

    from app.db.mongo import COLLECTION_AGENT_AUDIT_LOG, get_mongo_db

    try:
        db = get_mongo_db()
        await db[COLLECTION_AGENT_AUDIT_LOG].insert_one(
            {
                "agent_name": agent_name,
                "user_id": user_id,
                "final_text": result.final_text,
                "trace": [
                    {"tool_name": t.tool_name, "tool_input": t.tool_input, "tool_result": _stringify(t.tool_result)}
                    for t in result.trace
                ],
                "requires_confirmation": result.requires_confirmation,
                "proposed_action": result.proposed_action,
                "turns_used": result.turns_used,
                "extra": extra or {},
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:
        pass  # best-effort; never fail the caller's request because audit logging failed
