from typing import Annotated

from langchain.tools import tool
from langgraph.prebuilt import InjectedState


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


# Retrieval is performed deterministically in call_model. The search tool was
# removed: exposing it only added an extra LLM round-trip (+1-3s) and let the
# model bypass the single retrieval pass.
agent_tools = [create_support_ticket]
