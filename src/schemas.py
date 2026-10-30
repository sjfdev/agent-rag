"""Pydantic models shared by the API layer and the chat service."""

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    user_dept: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    history: List[HistoryItem] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    response: str
    sources: List[Dict[str, Any]] = []
    timings: Dict[str, Any] = {}
    request_id: str = ""
