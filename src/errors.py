"""Exception classification shared by the chat service and the agent graph."""

# Vector-store connection failures (Milvus down, FAISS index unreadable).
try:
    from pymilvus.exceptions import MilvusException
except Exception:  # pragma: no cover
    MilvusException = ()

try:  # openai is a hard dependency of langchain-openai
    from openai import APIConnectionError, APITimeoutError
except Exception:  # pragma: no cover
    APIConnectionError = APITimeoutError = ()


def is_upstream_error(exc: Exception) -> bool:
    """Network/DNS/timeout failures while talking to the LLM provider."""
    if APIConnectionError and isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(
        names & {"ConnectError", "ReadTimeout", "ConnectTimeout", "APITimeoutError"}
    )


def is_store_error(exc: Exception) -> bool:
    """The vector store is unreachable (as opposed to a bad request or a bug)."""
    if MilvusException and isinstance(exc, MilvusException):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"ConnectionError", "ConnectionRefusedError", "MilvusException"})


def classify_error(exc: Exception):
    """Map an exception to (http_status, code, message)."""
    if type(exc).__name__ in {"ChatTimeout", "FutureTimeout", "TimeoutError"}:
        return 504, "timeout", "request timed out"
    if is_store_error(exc):
        return 503, "vector_store_unavailable", "vector store unavailable"
    if is_upstream_error(exc):
        return 503, "upstream_unavailable", "llm upstream unavailable"
    return 500, "internal_error", "internal error"
