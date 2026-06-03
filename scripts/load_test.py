"""
scripts/load_test.py
~~~~~~~~~~~~~~~~~~~~
Locust load-test scenario for Context-Ring.

Simulates a realistic agent-swarm workload:
  - Each virtual user represents a long-running LLM session.
  - Every task sends a chat-completion request with a consistent session_id.
  - A small fraction of users register / deregister nodes to stress dynamic
    ring mutations under load.

Run:
    pip install locust
    locust -f scripts/load_test.py --host http://localhost:8000
    # Then open http://localhost:8089 to drive the test.

Or headless:
    locust -f scripts/load_test.py --host http://localhost:8000 \
        --headless -u 50 -r 10 --run-time 60s
"""

import random
import uuid

from locust import HttpUser, between, task


ADMIN_HEADERS = {"X-Api-Key": "change-me-in-production"}
SAMPLE_MODELS = ["gpt-4o", "claude-3-5-sonnet-20241022", "gemini-1.5-pro"]


class SessionUser(HttpUser):
    """
    Simulates an AI agent client with a persistent session.

    Each user sends repeated chat requests with the same session_id so that
    Context-Ring should consistently route them to the same agent.
    """

    wait_time = between(0.1, 1.0)

    def on_start(self):
        self.session_id = f"locust-{uuid.uuid4().hex[:12]}"
        self.model = random.choice(SAMPLE_MODELS)
        self.turn = 0

    @task(10)
    def send_chat_completion(self):
        self.turn += 1
        payload = {
            "session_id": self.session_id,
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": f"Turn {self.turn}: tell me something interesting."},
            ],
        }
        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            catch_response=True,
            name="/v1/chat/completions",
        ) as resp:
            if resp.status_code == 502:
                # Expected when no real agent is running — mark as success for routing test
                resp.success()
            elif resp.status_code not in (200, 201):
                resp.failure(f"Unexpected {resp.status_code}: {resp.text[:120]}")

    @task(1)
    def check_ring_status(self):
        self.client.get("/v1/ring/status", name="/v1/ring/status")

    @task(1)
    def check_health(self):
        self.client.get("/healthz", name="/healthz")


class AdminUser(HttpUser):
    """
    Simulates infrastructure automation registering / deregistering nodes.
    Runs at low weight to stress ring mutations under read traffic.
    """

    wait_time = between(5, 15)
    weight = 1   # 1 admin for every N session users

    def on_start(self):
        self._registered: list[str] = []

    @task
    def register_then_deregister(self):
        port = random.randint(9000, 9999)
        url = f"http://dynamic-agent-{uuid.uuid4().hex[:6]}:{port}"

        resp = self.client.post(
            "/v1/register",
            json={"agent_url": url},
            headers=ADMIN_HEADERS,
            name="/v1/register",
        )
        if resp.status_code == 201:
            self._registered.append(url)

        # Deregister a previously added node
        if self._registered:
            to_remove = self._registered.pop(0)
            self.client.post(
                "/v1/deregister",
                json={"agent_url": to_remove},
                headers=ADMIN_HEADERS,
                name="/v1/deregister",
            )
