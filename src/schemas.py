"""Pydantic models shared by the API layer and the chat service."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    # Single-department callers keep using `user_dept`; users who span several
    # departments can send `user_depts` instead (or both, which is merged).
    user_dept: Optional[str] = Field(default=None, max_length=64)
    user_depts: List[str] = Field(default_factory=list, max_length=20)
    # Optional group membership for the document-level ACL.
    user_groups: List[str] = Field(default_factory=list, max_length=50)
    message: str = Field(min_length=1, max_length=4000)
    # Hard guard only (payload size / abuse). `HISTORY_MAX_MESSAGES` decides how
    # much of it is actually used, so ordinary long chats are trimmed, not
    # rejected.
    history: List[HistoryItem] = Field(default_factory=list, max_length=200)

    def departments(self) -> List[str]:
        """All requested departments, de-duplicated, in request order."""
        values = list(self.user_depts or [])
        if self.user_dept:
            values.insert(0, self.user_dept)
        return list(dict.fromkeys(values))


class ChatResponse(BaseModel):
    response: str
    sources: List[Dict[str, Any]] = []
    timings: Dict[str, Any] = {}
    request_id: str = ""
    # True when the answer came from retrieval because the LLM failed.
    degraded: bool = False
    degraded_reason: str = ""
