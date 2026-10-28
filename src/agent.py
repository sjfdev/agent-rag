import time
from typing import Annotated, TypedDict, List, Dict

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.config import settings
from src.engine import kb_engine
from src.tools import agent_tools


class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    user_dept: str
    user_id: str
    # Per-request instrumentation (ms), surfaced by the /chat endpoint.
    timings: Dict[str, float]


SYSTEM_PROMPT = """You are an enterprise knowledge base assistant.

Rules:
1. Answer factual/policy questions using ONLY the knowledge base excerpts
   provided below. Never use outside knowledge and never guess.
2. The excerpts are already filtered by the caller's department. If they do not
   contain the answer, say the information is not available for the user's
   department. Never speculate about other departments' data.
3. If the issue is a technical problem the knowledge base cannot solve
   (e.g. broken hardware), call create_support_ticket.
4. Reply in the user's language, concise, at most 120 words.
"""


llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    openai_api_base=settings.LLM_API_BASE,
    openai_api_key=settings.LLM_API_KEY,
    temperature=0,
    max_tokens=512,
).bind_tools(agent_tools)


def _latest_user_query(messages: List[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
    return ""


def call_model(state: AgentState):
    """Retrieve permission-filtered context, then let the LLM answer or call a tool."""
    messages = state["messages"]
    dept = state["user_dept"]
    timings = dict(state.get("timings") or {})

    prompt: List[BaseMessage] = [
        SystemMessage(
            content=SYSTEM_PROMPT
            + f"\nCurrent user: id={state['user_id']}, dept={dept}"
        )
    ]

    # Deterministic first-stage retrieval: permissions come from state, not the LLM.
    already_retrieved = any(isinstance(m, ToolMessage) for m in messages)
    if not already_retrieved:
        query = _latest_user_query(messages)
        t_search = time.perf_counter()
        docs = kb_engine.search(query, dept, k=4) if query else []
        timings["retrieval_ms"] = timings.get("retrieval_ms", 0.0) + (
            time.perf_counter() - t_search
        ) * 1000
        timings["retrieval_calls"] = timings.get("retrieval_calls", 0) + 1
        if docs:
            excerpts = "\n\n".join(
                f"[{doc.metadata.get('dept')}] {doc.page_content}" for doc in docs
            )
        else:
            excerpts = "(no relevant excerpts found)"
        prompt.append(
            SystemMessage(
                content=f"Knowledge base excerpts for dept {dept}:\n{excerpts}"
            )
        )

    t_llm = time.perf_counter()
    response = llm.invoke(prompt + messages)
    timings["llm_ms"] = timings.get("llm_ms", 0.0) + (
        time.perf_counter() - t_llm
    ) * 1000
    timings["llm_calls"] = timings.get("llm_calls", 0) + 1

    return {"messages": [response], "timings": timings}


workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(agent_tools))
workflow.set_entry_point("agent")


def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "tools"
    return END


workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

app = workflow.compile()
