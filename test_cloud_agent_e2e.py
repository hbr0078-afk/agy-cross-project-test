"""End-to-end test: official SDK Agent -> AntigravityCloudConnectionStrategy -> Antigravity Cloud API."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, "/home/ubuntu/agy-router")

from cloud_connection import AntigravityCloudConnectionStrategy, build_client_from_env
from client import AntigravityClient
from google.antigravity.agent import Agent
from google.antigravity.connections.connection import AgentConfig


class AntigravityAgentConfig(AgentConfig):
    """AgentConfig that wires chi's AntigravityClient into the SDK Agent."""

    client: AntigravityClient | None = None
    environment: str = "remote"
    environment_id: str | None = None
    model: str | None = None
    timeout: int = 120

    def create_strategy(
        self,
        *,
        tool_runner: Any,
        hook_runner: Any,
    ) -> Any:
        from cloud_connection import AntigravityCloudConnectionStrategy  # noqa: PLC0415

        return AntigravityCloudConnectionStrategy(
            client=self.client,
            environment=self.environment,
            environment_id=self.environment_id,
            model=self.model,
            timeout=self.timeout,
        )


async def main() -> None:
    api_key = os.environ.get("AGY_KEY_1") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("SKIP: no AGY_KEY_1 / GEMINI_API_KEY set")
        return

    client = build_client_from_env(api_key=api_key)
    config = AntigravityAgentConfig(
        client=client,
        environment="remote",
        model=None,
    )

    async with Agent(config=config) as agent:
        response = await agent.chat("안녕? 짧은 인사말 한 줄만 해줘.")

    text = await response.text()
    print("응답 텍스트:", text)


if __name__ == "__main__":
    asyncio.run(main())
