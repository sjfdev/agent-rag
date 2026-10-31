"""Exception classification shared by the chat service and the agent graph."""

class ChatTimeout(Exception):
    """The request exceeded CHAT_TIMEOUT before the agent finished."""


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


def is_transport_error(exc: Exception) -> bool:
    """Any transport-level failure (used when the call target is already known).

    `is_upstream_error` is tuned for the chat endpoint's shared classification;
    inside `call_model` we know the call is to the LLM, so a bare
    ConnectionError/TimeoutError also means "the model is unreachable".
    """
    return (
        is_upstream_error(exc)
        or is_timeout(exc)
        or isinstance(exc, (ConnectionError, TimeoutError, OSError))
    )


def is_timeout(exc: Exception) -> bool:
    return isinstance(exc, ChatTimeout) or type(exc).__name__ in {
        "FutureTimeout",
        "TimeoutError",
    }


def classify_error(exc: Exception):
    """Map an exception to (http_status, code, message)."""
    if is_timeout(exc):
        return 504, "timeout", "request timed out"
    if is_store_error(exc):
        return 503, "vector_store_unavailable", "vector store unavailable"
    if is_upstream_error(exc):
        return 503, "upstream_unavailable", "llm upstream unavailable"
    return 500, "internal_error", "internal error"
