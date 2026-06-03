"""
examples/langgraph_integration.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Demonstrates routing a LangGraph StateGraph workflow through
Context-Ring so each graph execution consistently hits the same
agent worker across all node invocations.

Prerequisites:
    pip install langgraph langchain-openai
    docker compose up context-ring-proxy

Usage:
    OPENAI_API_KEY=sk-... python examples/langgraph_integration.py

How it works:
    The LangChain ChatOpenAI client is pointed at the Context-Ring
    proxy URL instead of api.openai.com.  A custom header factory
    injects the workflow's run_id as X-Session-ID so every LLM call
    within the same graph execution lands on the same agent node.
"""

from __future__ import annotations

import os
import uuid
from typing import TypedDict

# LangGraph / LangChain imports (only available if installed)
try:
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    print("LangGraph not installed. Install with: pip install langgraph langchain-openai")


PROXY_URL = os.getenv("CONTEXT_RING_PROXY_URL", "http://localhost:8000/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-placeholder")


# ─── Graph state ──────────────────────────────────────────────────────────────

class WorkflowState(TypedDict):
    session_id: str
    user_input: str
    analysis: str
    final_answer: str


# ─── Node implementations ─────────────────────────────────────────────────────

def make_llm(session_id: str) -> "ChatOpenAI":
    """
    Build a ChatOpenAI client that routes through Context-Ring.

    The session_id is embedded in default_headers so every call from
    this LLM instance carries the same X-Session-ID header, keeping
    all turns pinned to the same agent worker.
    """
    return ChatOpenAI(
        model="gpt-4o",
        openai_api_base=PROXY_URL,
        openai_api_key=OPENAI_API_KEY,
        default_headers={"X-Session-ID": session_id},
        streaming=False,
    )


def analysis_node(state: WorkflowState) -> WorkflowState:
    """Node 1: Analyse the user's input."""
    llm = make_llm(state["session_id"])
    response = llm.invoke([
        SystemMessage(content="You are an expert analyst. Be concise."),
        HumanMessage(content=f"Analyse this request: {state['user_input']}"),
    ])
    return {**state, "analysis": response.content}


def answer_node(state: WorkflowState) -> WorkflowState:
    """Node 2: Generate a final answer using the analysis."""
    llm = make_llm(state["session_id"])
    response = llm.invoke([
        SystemMessage(content="You are a helpful assistant. Use the provided analysis."),
        HumanMessage(content=(
            f"Original request: {state['user_input']}\n\n"
            f"Analysis: {state['analysis']}\n\n"
            "Provide a clear final answer."
        )),
    ])
    return {**state, "final_answer": response.content}


# ─── Graph construction ───────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("analyse", analysis_node)
    graph.add_node("answer", answer_node)
    graph.set_entry_point("analyse")
    graph.add_edge("analyse", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


# ─── Runner ───────────────────────────────────────────────────────────────────

def run_workflow(user_input: str) -> str:
    """
    Execute the graph for a user request.

    A stable session_id is derived from the input so repeated runs
    of the same logical task always route to the same agent (and hit
    the same warm context cache).
    """
    # Deterministic session per logical task — change to uuid4() for unique sessions
    session_id = f"langgraph:{uuid.uuid5(uuid.NAMESPACE_DNS, user_input).hex[:16]}"
    print(f"  session_id: {session_id}")

    compiled = build_graph()
    result = compiled.invoke({
        "session_id": session_id,
        "user_input": user_input,
        "analysis": "",
        "final_answer": "",
    })
    return result["final_answer"]


def main():
    if not LANGGRAPH_AVAILABLE:
        return

    questions = [
        "What are the key differences between consistent hashing and rendezvous hashing?",
        "How does MurmurHash3 compare to FNV-1a for hash ring use cases?",
    ]

    print("\n── Context-Ring + LangGraph Demo ──\n")
    for q in questions:
        print(f"Q: {q}")
        answer = run_workflow(q)
        print(f"A: {answer[:200]}{'...' if len(answer) > 200 else ''}\n")


if __name__ == "__main__":
    main()
