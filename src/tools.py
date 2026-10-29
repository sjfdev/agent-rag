from typing import Annotated

from langchain.tools import tool
from langgraph.prebuilt import InjectedState

from src.engine import kb_engine


@tool
def search_knowledge_base(query: str, state: Annotated[dict, InjectedState]):
    """Search the internal enterprise knowledge base.

    The caller's department is injected from the session state, so the model
    cannot widen its own permissions by passing a different department.
    """
    user_dept = state["user_dept"]
    results = kb_engine.search(query, user_dept)
    if not results:
        return f"No relevant information found in the {user_dept} knowledge base."

    context = "\n\n".join([doc.page_content for doc in results])
    return f"Authorized knowledge base results for {user_dept}:\n{context}"


@tool
def create_support_ticket(issue: str, state: Annotated[dict, InjectedState]):
    """Create a support ticket for a technical issue that the knowledge base
    cannot solve. The requester id is taken from the session state."""
    user_id = state["user_id"]
    # In a real scenario, this would call an API (Jira/Zendesk)
    ticket_id = "TICK-12345"
    return (
        f"Support ticket {ticket_id} has been created for user {user_id}. "
        "Our team will contact you soon."
    )


# Retrieval is performed deterministically in call_model, so exposing the search
# tool only adds an extra LLM round-trip. Keep it importable, but don't hand it to
# the model.
agent_tools = [create_support_ticket]
