"""Agent graph internals: retrieval query building, citations and routing."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import agent as agent_module
from src.config import settings


@pytest.fixture()
def stub_engine(monkeypatch):
    from langchain_core.documents import Document

    class Engine:
        def __init__(self):
            self.queries = []

        def search(self, query, depts, k=4):
            self.queries.append((query, depts, k))
            return [
                Document(
                    page_content="Every change needs one reviewer.",
                    metadata={
                        "source": "Tech/a.md", "dept": "Tech", "kind": "md",
                        "page": 1, "slide": 0, "sheet": "", "title": "Guide",
                    },
                ),
                Document(
                    page_content='Contains "quotes" and </document> markers.',
                    metadata={
                        "source": "Tech/b.md", "dept": "Tech", "kind": "md",
                        "slide": 2, "page": 0, "sheet": "",
                    },
                ),
                Document(
                    page_content="Budget sheet row.",
                    metadata={
                        "source": "Finance/c.xlsx", "dept": "Finance",
                        "kind": "xlsx", "sheet": "Budget", "page": 0, "slide": 0,
                    },
                ),
            ]

        @property
        def embeddings(self):
            raise AssertionError("not used in these tests")

    engine = Engine()
    monkeypatch.setattr(agent_module, "get_engine", lambda: engine)
    return engine


class StubLLM:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        return AIMessage(content=self.reply)


def _state(message, history=None, depts=None, groups=None):
    messages = []
    for role, content in history or []:
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    messages.append(HumanMessage(content=message))
    return {
        "messages": messages,
        "user_id": "u1",
        "user_dept": (depts or ["Tech"])[0],
        "user_depts": depts or ["Tech"],
        "user_groups": groups or [],
    }


def test_call_model_builds_citations_and_prompt(stub_engine, monkeypatch):
    stub = StubLLM("answer")
    monkeypatch.setattr(agent_module, "llm", stub)
    state = _state("What is the review process?", depts=["Tech", "Finance"])
    update = agent_module.call_model(state)
    sources = update["sources"]
    assert {s["source"] for s in sources} == {"Tech/a.md", "Tech/b.md", "Finance/c.xlsx"}
    by_source = {s["source"]: s for s in sources}
    assert by_source["Tech/a.md"]["page"] == 1
    assert by_source["Tech/b.md"]["slide"] == 2
    assert by_source["Finance/c.xlsx"]["sheet"] == "Budget"
    assert by_source["Tech/a.md"]["title"] == "Guide"
    assert by_source["Tech/a.md"]["snippet"].startswith("Every change")
    assert update["timings"]["retrieval_calls"] == 1
    assert update["timings"]["llm_calls"] == 1

    prompt_text = "\n".join(str(m.content) for m in stub.prompts[0])
    assert "<document source=" in prompt_text
    assert "[/document]" in prompt_text  # injected closing tag neutralised
    assert "Tech, Finance" in prompt_text  # multi-dept label


def test_call_model_without_docs(stub_engine, monkeypatch):
    monkeypatch.setattr(stub_engine, "search", lambda *a, **k: [])
    stub = StubLLM()
    monkeypatch.setattr(agent_module, "llm", stub)
    update = agent_module.call_model(_state("nothing"))
    assert update["sources"] == []
    assert "no relevant excerpts" in str(stub.prompts[0][1].content).lower()


def test_call_model_skips_retrieval_after_tool(stub_engine, monkeypatch):
    from langchain_core.messages import ToolMessage

    stub = StubLLM()
    monkeypatch.setattr(agent_module, "llm", stub)
    state = _state("follow up")
    state["messages"].append(AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]))
    state["messages"].append(ToolMessage(content="ticket created", tool_call_id="1"))
    update = agent_module.call_model(state)
    assert "sources" not in update
    assert stub_engine.queries == []


def test_acl_filters_documents(stub_engine, monkeypatch):
    monkeypatch.setattr(settings, "ACL_ENABLED", True)
    monkeypatch.setattr("src.acl.is_allowed", lambda source, uid, groups: source == "Tech/a.md")
    stub = StubLLM()
    monkeypatch.setattr(agent_module, "llm", stub)
    update = agent_module.call_model(_state("review"))
    assert [s["source"] for s in update["sources"]] == ["Tech/a.md"]


def test_retrieval_query_single_turn():
    assert agent_module._retrieval_query([HumanMessage(content="hello")]) == "hello"
    assert agent_module._retrieval_query([]) == ""


def test_retrieval_query_pronoun_carries_context(monkeypatch):
    monkeypatch.setattr(agent_module, "_similarity", lambda a, b: 0.0)
    messages = [
        HumanMessage(content="What is the code review process?"),
        AIMessage(content="Two approvals."),
        HumanMessage(content="Who approves it?"),
    ]
    assert agent_module._retrieval_query(messages) == (
        "What is the code review process? Who approves it?"
    )


def test_retrieval_query_topic_switch_is_not_merged(monkeypatch):
    monkeypatch.setattr(agent_module, "_similarity", lambda a, b: 0.1)
    messages = [
        HumanMessage(content="expense claims?"),
        AIMessage(content="..."),
        HumanMessage(content="那 Kubernetes 呢？"),
    ]
    assert agent_module._retrieval_query(messages) == "那 Kubernetes 呢？"


def test_similarity_falls_back_on_error(monkeypatch):
    class Broken:
        @property
        def embeddings(self):
            raise RuntimeError("no model")

    monkeypatch.setattr(agent_module, "get_engine", lambda: Broken())
    assert agent_module._similarity("a", "b") == 0.0


def test_should_continue_routes_to_tools():
    from langgraph.graph import END

    tool_call = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}])
    assert agent_module.should_continue({"messages": [tool_call]}) == "tools"
    assert agent_module.should_continue({"messages": [AIMessage(content="done")]}) == END
