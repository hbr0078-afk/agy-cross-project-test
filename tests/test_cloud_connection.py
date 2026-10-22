"""Unit tests for AntigravityCloudConnection / AntigravityCloudConnectionStrategy.

Uses a fake AntigravityClient so no real API call is needed.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any, Dict, Optional

sys.path.insert(0, "/home/ubuntu/agy-router")

from cloud_connection import (
    AntigravityCloudConnection,
    AntigravityCloudConnectionStrategy,
    build_client_from_env,
)
from client import AntigravityClient
from google.antigravity import types


# --------------------------------------------------------------------------- #
# Fake client
# --------------------------------------------------------------------------- #

class FakeAntigravityClient(AntigravityClient):
    """Stand-in that returns canned results instead of calling the API."""

    def __init__(self, result: Dict[str, Any]):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create_interaction(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return dict(self.result)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_success_result() -> Dict[str, Any]:
    return {
        "success": True,
        "status_code": 200,
        "status": "OK",
        "environment_id": "env_123",
        "interaction_id": "int_456",
        "usage": {
            "prompt_token_count": 5,
            "candidates_token_count": 8,
            "total_token_count": 13,
        },
        "output": "안녕하세요! 연결이 성공적으로 확인되었습니다.",
    }


def make_error_result(status_code: int = 401) -> Dict[str, Any]:
    return {
        "success": False,
        "status_code": status_code,
        "error": {"message": "API key not valid."},
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

async def test_send_receive_success() -> None:
    client = FakeAntigravityClient(make_success_result())
    conn = AntigravityCloudConnection(client=client)
    await conn.start()

    await conn.send("안녕?")
    steps = [step async for step in conn.receive_steps()]

    assert len(steps) == 1, f"expected 1 step, got {len(steps)}"
    step = steps[0]
    assert step.type == types.StepType.TEXT_RESPONSE, step.type
    assert step.source == types.StepSource.MODEL
    assert step.target == types.StepTarget.USER
    assert step.status == types.StepStatus.DONE
    assert step.content == "안녕하세요! 연결이 성공적으로 확인되었습니다."
    assert step.is_complete_response is True
    assert step.trajectory_id == "env_123"
    assert step.id == "int_456"

    # Usage metadata attached.
    assert step.usage_metadata is not None
    assert step.usage_metadata.total_token_count == 13

    await conn.disconnect()
    print("PASS test_send_receive_success")


async def test_send_receive_error() -> None:
    client = FakeAntigravityClient(make_error_result(401))
    conn = AntigravityCloudConnection(client=client)
    await conn.start()

    await conn.send("테스트")
    steps = [step async for step in conn.receive_steps()]

    assert len(steps) == 1
    step = steps[0]
    assert step.type == types.StepType.SYSTEM_MESSAGE
    assert step.status == types.StepStatus.ERROR
    assert "401" in step.content
    assert step.is_complete_response is True

    await conn.disconnect()
    print("PASS test_send_receive_error")


async def test_strategy_connect() -> None:
    client = FakeAntigravityClient(make_success_result())
    strategy = AntigravityCloudConnectionStrategy(client=client)
    conn = strategy.connect()
    assert isinstance(conn, AntigravityCloudConnection)
    print("PASS test_strategy_connect")


async def test_context_manager() -> None:
    client = FakeAntigravityClient(make_success_result())
    async with AntigravityCloudConnection(client=client) as conn:
        await conn.send("컨텍스트 매니저 테스트")
        steps = [step async for step in conn.receive_steps()]
        assert len(steps) == 1
        assert steps[0].content == "안녕하세요! 연결이 성공적으로 확인되었습니다."
    print("PASS test_context_manager")


async def test_cancel_clears_state() -> None:
    client = FakeAntigravityClient(make_success_result())
    conn = AntigravityCloudConnection(client=client, environment_id="env_old")
    await conn.start()
    await conn.cancel()
    assert conn._environment_id is None
    assert conn._previous_interaction_id is None
    await conn.disconnect()
    print("PASS test_cancel_clears_state")


async def test_wait_for_wakeup_unsupported() -> None:
    client = FakeAntigravityClient(make_success_result())
    conn = AntigravityCloudConnection(client=client)
    result = await conn.wait_for_wakeup()
    assert result is False
    print("PASS test_wait_for_wakeup_unsupported")


async def main() -> None:
    await test_send_receive_success()
    await test_send_receive_error()
    await test_strategy_connect()
    await test_context_manager()
    await test_cancel_clears_state()
    await test_wait_for_wakeup_unsupported()
    print("\nAll tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
