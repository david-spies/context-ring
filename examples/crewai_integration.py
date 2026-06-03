"""
examples/crewai_integration.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shows how to wire Context-Ring into a CrewAI agent swarm so that
every agent turn for the same task is routed to the same worker,
preserving local chat history and eliminating redundant context loads.

Prerequisites:
    pip install crewai openai
    docker compose up context-ring-proxy agent-worker-1 agent-worker-2

Usage:
    python examples/crewai_integration.py
"""

from __future__ import annotations

import os
import uuid
import httpx


# ─── Context-Ring client ──────────────────────────────────────────────────────

class ContextRingClient:
    """
    Minimal async client for interacting with the Context-Ring proxy.

    In production, replace with your framework's HTTP client or the
    requests / httpx library depending on your sync/async setup.
    """

    def __init__(
        self,
        proxy_url: str = "http://localhost:8000",
        api_key: str = "",
    ):
        self.proxy_url = proxy_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        }

    def register(self, agent_url: str) -> dict:
        resp = httpx.post(
            f"{self.proxy_url}/v1/register",
            json={"agent_url": agent_url},
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def deregister(self, agent_url: str) -> dict:
        resp = httpx.post(
            f"{self.proxy_url}/v1/deregister",
            json={"agent_url": agent_url},
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def chat(self, session_id: str, messages: list, model: str = "gpt-4o") -> dict:
        """
        Route a chat-completion request through the proxy.
        session_id ensures the request reaches the same agent every time.
        """
        resp = httpx.post(
            f"{self.proxy_url}/v1/chat/completions",
            json={
                "session_id": session_id,
                "model": model,
                "messages": messages,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def status(self) -> dict:
        resp = httpx.get(f"{self.proxy_url}/v1/ring/status", timeout=5)
        resp.raise_for_status()
        return resp.json()


# ─── Simulated CrewAI task with persistent sessions ───────────────────────────

class PersistentAgentTask:
    """
    Wraps a multi-turn LLM task with a stable session_id so Context-Ring
    routes every turn to the same agent instance.
    """

    def __init__(self, task_name: str, client: ContextRingClient):
        # Deterministic session ID per task — survives retries without
        # causing a new cache miss on the same logical work item.
        self.session_id = f"task:{task_name}:{uuid.uuid5(uuid.NAMESPACE_DNS, task_name).hex[:12]}"
        self.client = client
        self.history: list[dict] = []
        self.task_name = task_name
        print(f"  [Task:{task_name}]  session_id={self.session_id}")

    def run_turn(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        try:
            response = self.client.chat(
                session_id=self.session_id,
                messages=self.history,
            )
            assistant_msg = response["choices"][0]["message"]["content"]
            self.history.append({"role": "assistant", "content": assistant_msg})
            node = response.get("agent", "unknown")
            print(f"  [Turn {len(self.history)//2}]  routed → {node}")
            return assistant_msg
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 502:
                print(f"  [Turn] Agent unreachable (502) — will retry next turn via remapped node")
                return ""
            raise


# ─── Demo ─────────────────────────────────────────────────────────────────────

def main():
    PROXY = os.getenv("CONTEXT_RING_PROXY_URL", "http://localhost:8000")
    KEY = os.getenv("CONTEXT_RING_API_KEY", "dev-key")

    client = ContextRingClient(proxy_url=PROXY, api_key=KEY)

    # 1. Register agent workers (in production this is handled by your
    #    K8s controller / ECS task lifecycle hooks)
    print("\n── Registering agent nodes ──")
    for url in ["http://agent-worker-1:8001", "http://agent-worker-2:8001"]:
        try:
            r = client.register(url)
            print(f"  ✓  {r['status']}  {url}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                print(f"  (already registered)  {url}")
            else:
                raise

    # 2. Show ring status
    print("\n── Ring status ──")
    status = client.status()
    for node in status["nodes"]:
        print(f"  {node['url']}  arc={node['arc_fraction']*100:.1f}%")

    # 3. Run two independent tasks
    #    Each task gets its own session_id, so they can run on different nodes.
    #    Within each task, every turn routes to the same node → cache hits.
    print("\n── Running multi-turn tasks ──")

    task_a = PersistentAgentTask("code-review-pr-42", client)
    task_b = PersistentAgentTask("data-analysis-q3", client)

    # Task A: 3 turns — should all land on the same agent
    task_a.run_turn("Review this Python function for correctness and style.")
    task_a.run_turn("Now add type annotations.")
    task_a.run_turn("Write a docstring for it.")

    # Task B: 2 turns — may land on a different agent from Task A (by design)
    task_b.run_turn("Summarise the Q3 revenue figures.")
    task_b.run_turn("Highlight the top-performing regions.")

    print("\n── Done ──\n")


if __name__ == "__main__":
    main()
