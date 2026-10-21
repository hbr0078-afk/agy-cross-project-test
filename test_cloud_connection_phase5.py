"""Phase 5: Key rotation, error recovery, tool-call mapping, regression tests.

Scope: tests cloud_connection.py's own responsibilities (Step conversion,
session/environment stickiness, cancel, strategy connect). Retry/key-rotation
logic lives in chi's AntigravityClient and is exercised by chi's own test
suite (test_client_pool.py etc.).

Runs without pytest — plain asyncio + asserts.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Optional

sys.path.insert(0, "/home/ubuntu/agy-router")

from cloud_connection import (
    AntigravityCloudConnection,
    AntigravityCloudConnectionStrategy,
)
from client import AntigravityClient
from google.antigravity import types


# --------------------------------------------------------------------------- #
# Fake client: returns canned results from create_interaction
# --------------------------------------------------------------------------- #

class ScenarioClient(AntigravityClient):
    """Returns canned results from create_interaction in sequence."""

    def __init__(self, results: list[Dict[str, Any]]):
        self._api_key = "fake-key"
        self._key_pool = None
        self.project_id = None
        self.registry = None
        self.session_manager = None
        self.round_robin = None
        self.model = None

        self._results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.call_count = 0

    def create_interaction(self, **kwargs: Any) -> Dict[str, Any]:
        self.call_count += 1
        self.calls.append(dict(kwargs))
        if self._results:
            return dict(self._results.pop(0))
        return {
            "success": False,
            "status_code": 500,
            "error": {"message": "no more scenarios"},
        }


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

SUCCESS = {
    "success": True,
    "status_code": 200,
    "status": "OK",
    "environment_id": "env_alpha",
    "interaction_id": "int_1",
    "usage": {"prompt_token_count": 3, "candidates_token_count": 6, "total_token_count": 9},
    "output": "첫 번째 응답입니다.",
}

FOLLOWUP_SUCCESS = {
    "success": True,
    "status_code": 200,
    "status": "OK",
    "environment_id": "env_alpha",
    "interaction_id": "int_2",
    "usage": {"prompt_token_count": 4, "candidates_token_count": 7, "total_token_count": 11},
    "output": "후속 응답입니다. 환경이 유지됩니다.",
}

FINAL_ERROR_401 = {
    "success": False,
    "status_code": 401,
    "error": {"message": "API key not valid."},
}

SERVER_500 = {
    "success": False,
    "status_code": 500,
    "error": {"message": "Internal server error."},
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def collect_one_step(
    conn: AntigravityCloudConnection, prompt: str = "테스트"
) -> types.Step:
    await conn.send(prompt)
    steps = [step async for step in conn.receive_steps()]
    assert len(steps) == 1, f"expected 1 step, got {len(steps)}"
    return steps[0]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

async def test_success_converts_to_text_response_step() -> None:
    """성공 응답이 TEXT_RESPONSE / MODEL / DONE 스텝으로 변환되는지 확인."""
    client = ScenarioClient([SUCCESS])
    conn = AntigravityCloudConnection(client=client)
    await conn.start()

    step = await collect_one_step(conn, "성공 테스트")

    assert step.type == types.StepType.TEXT_RESPONSE
    assert step.source == types.StepSource.MODEL
    assert step.target == types.StepTarget.USER
    assert step.status == types.StepStatus.DONE
    assert step.content == "첫 번째 응답입니다."
    assert step.is_complete_response is True
    assert step.trajectory_id == "env_alpha"
    assert step.id == "int_1"
    assert step.usage_metadata is not None
    assert step.usage_metadata.total_token_count == 9

    await conn.disconnect()
    print("PASS test_success_converts_to_text_response_step")


async def test_error_converts_to_system_message_step() -> None:
    """최종 실패 응답이 SYSTEM_MESSAGE / ERROR 스텝으로 변환되는지 확인."""
    client = ScenarioClient([FINAL_ERROR_401])
    conn = AntigravityCloudConnection(client=client)
    await conn.start()

    step = await collect_one_step(conn, "에러 테스트")

    assert step.type == types.StepType.SYSTEM_MESSAGE
    assert step.source == types.StepSource.SYSTEM
    assert step.status == types.StepStatus.ERROR
    assert "401" in step.content
    assert step.is_complete_response is True

    await conn.disconnect()
    print("PASS test_error_converts_to_system_message_step")


async def test_no_tool_call_steps_synthesized() -> None:
    """도구 호출: 클라우드 응답에 tool_calls가 없어도 TOOL_CALL 스텝을 합성하지 않음."""
    client = ScenarioClient([SUCCESS])
    conn = AntigravityCloudConnection(client=client)
    await conn.start()

    step = await collect_one_step(conn, "도구 호출 없음 테스트")

    assert step.type == types.StepType.TEXT_RESPONSE
    assert step.tool_calls == [], f"expected no tool_calls, got {step.tool_calls}"

    await conn.disconnect()
    print("PASS test_no_tool_call_steps_synthesized")


async def test_session_environment_stickiness_across_turns() -> None:
    """세션 환경 끈적임: 두 번째 턴도 environment_id를 유지해야 함."""
    client = ScenarioClient([SUCCESS, FOLLOWUP_SUCCESS])
    conn = AntigravityCloudConnection(
        client=client,
        session_id="sess_sticky",
        environment_id="env_sticky",
    )
    await conn.start()

    step1 = await collect_one_step(conn, "첫 턴")
    assert step1.trajectory_id == "env_alpha"
    assert step1.content == "첫 번째 응답입니다."

    step2 = await collect_one_step(conn, "두 번째 턴")
    assert step2.trajectory_id == "env_alpha"
    assert step2.content == "후속 응답입니다. 환경이 유지됩니다."
    assert client.call_count == 2

    await conn.disconnect()
    print("PASS test_session_environment_stickiness_across_turns")


async def test_cancel_invalidates_interaction_id() -> None:
    """취소: cancel() 후 environment_id와 previous_interaction_id가 None이어야 함."""
    client = ScenarioClient([SUCCESS])
    conn = AntigravityCloudConnection(
        client=client,
        environment_id="env_before_cancel",
        previous_interaction_id="int_before_cancel",
    )
    await conn.start()

    await conn.cancel()
    assert conn._environment_id is None
    assert conn._previous_interaction_id is None

    await conn.disconnect()
    print("PASS test_cancel_invalidates_interaction_id")


async def test_context_manager_lifecycle() -> None:
    """컨텍스트 매니저: enter/exit이 정상 동작해야 함."""
    client = ScenarioClient([SUCCESS])
    async with AntigravityCloudConnection(client=client) as conn:
        await conn.send("컨텍스트 매니저 테스트")
        steps = [step async for step in conn.receive_steps()]
        assert len(steps) == 1
        assert steps[0].content == "첫 번째 응답입니다."
    print("PASS test_context_manager_lifecycle")


async def test_wait_for_wakeup_unsupported() -> None:
    """웨이크업: 클라우드 직접 모드는 미지원(False) 반환."""
    client = ScenarioClient([SUCCESS])
    conn = AntigravityCloudConnection(client=client)
    result = await conn.wait_for_wakeup()
    assert result is False
    print("PASS test_wait_for_wakeup_unsupported")


async def test_strategy_connect_returns_connection() -> None:
    """전략: connect()가 AntigravityCloudConnection 인스턴스를 반환해야 함."""
    client = ScenarioClient([SUCCESS])
    strategy = AntigravityCloudConnectionStrategy(client=client)
    conn = strategy.connect()
    assert isinstance(conn, AntigravityCloudConnection)
    print("PASS test_strategy_connect_returns_connection")


async def test_regression_basic_send_receive() -> None:
    """회귀: 기본 send → receive_steps 흐름이 여전히 동작해야 함."""
    client = ScenarioClient([SUCCESS])
    conn = AntigravityCloudConnection(client=client)
    await conn.start()

    await conn.send("회귀 테스트")
    steps = [step async for step in conn.receive_steps()]

    assert len(steps) == 1
    assert steps[0].type == types.StepType.TEXT_RESPONSE
    assert steps[0].is_complete_response is True

    await conn.disconnect()
    print("PASS test_regression_basic_send_receive")


async def main() -> None:
    await test_success_converts_to_text_response_step()
    await test_error_converts_to_system_message_step()
    await test_no_tool_call_steps_synthesized()
    await test_session_environment_stickiness_across_turns()
    await test_cancel_invalidates_interaction_id()
    await test_context_manager_lifecycle()
    await test_wait_for_wakeup_unsupported()
    await test_strategy_connect_returns_connection()
    await test_regression_basic_send_receive()
    print("\n=== Phase 5: All tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
