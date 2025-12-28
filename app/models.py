from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[Any] = None
    tool_call_id: Optional[str] = None


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Dict[str, Any]


class ToolSpec(BaseModel):
    type: str = "function"
    function: ToolFunction


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    tools: Optional[List[ToolSpec]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False


class EmbeddingsRequest(BaseModel):
    model: str
    input: Any


class CompletionRequest(BaseModel):
    model: str
    prompt: Any
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: Optional[bool] = False


class RerankRequest(BaseModel):
    model: Optional[str] = None
    query: str
    documents: List[str]
    top_n: Optional[int] = None


class MemoryUpsertRequest(BaseModel):
    type: Literal["fact", "preference", "project", "ephemeral"]
    text: str
    source: Optional[Literal["user", "system", "tool"]] = "user"
    meta: Optional[Dict[str, Any]] = None
    id: Optional[str] = None
    ts: Optional[int] = None


class MemorySearchRequest(BaseModel):
    query: str
    types: Optional[List[Literal["fact", "preference", "project", "ephemeral"]]] = None
    sources: Optional[List[Literal["user", "system", "tool"]]] = None
    top_k: Optional[int] = None
    min_sim: Optional[float] = None
    max_age_sec: Optional[int] = None
    include_compacted: bool = False


class MemoryCompactRequest(BaseModel):
    types: Optional[List[Literal["fact", "preference", "project", "ephemeral"]]] = None
    max_age_sec: Optional[int] = None
    max_items: int = 50
    target_type: Literal["fact", "preference", "project", "ephemeral"] = "project"
    target_source: Literal["user", "system", "tool"] = "system"
    include_compacted: bool = False


class ToolExecRequest(BaseModel):
    arguments: Dict[str, Any] = {}
