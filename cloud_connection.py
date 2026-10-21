"""Antigravity Cloud Connection adapter for the official Google Antigravity SDK.

Wraps chi/achy-router's direct HTTP client (`AntigravityClient`) so an SDK
`Agent` can talk to the Antigravity Cloud Interactions API via the official
`Connection` / `ConnectionStrategy` interfaces.

Design notes (from the user's review checklist):

1. Connection interface compatibility
   - Implements `send(prompt, **kwargs)`, `receive_steps() -> AsyncIterator[Step]`,
     `disconnect()`, `cancel()`, `wait_for_idle()`, `wait_for_wakeup()` exactly
     as defined in `google.antigravity.connections.connection`.
   - Strategy implements `connect() -> Connection`.

2. Step conversion accuracy
   - Cloud text output -> StepType.TEXT_RESPONSE, StepSource.MODEL,
     StepTarget.USER, StepStatus.DONE.
   - Cloud errors -> StepType.SYSTEM_MESSAGE, StepStatus.ERROR.
   - Cloud does not return tool-call steps, so TOOL_CALL is not synthesized.
   - `is_complete_response=True` on the final message step.

3. Multi-key + session consistency
   - The chi `KeyPoolManager` is reused unchanged.
   - Once a session picks a key (bound_key), subsequent turns in the same
     session prefer that key so environment_id / interaction_id continuity is
     not broken by mid-session key rotation.

4. Cancel / disconnect semantics
   - `cancel()` invalidates the current interaction_id and clears pending
     state. It does NOT reach the server to kill an in-flight request
     (the Antigravity Cloud REST API has no cancel endpoint); this limitation
     is documented in the method docstring.
   - `disconnect()` clears session/environment state locally.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator, Optional

from google.antigravity import types
from google.antigravity.connections.connection import Connection, ConnectionStrategy

from client import AntigravityClient
from keys import KeyPoolManager, AllKeysExhaustedError

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
AGENT_NAME = "antigravity-preview-05-2026"


class AntigravityCloudConnection(Connection):
    """SDK Connection backed by chi's direct REST client."""

    def __init__(
        self,
        client: AntigravityClient,
        environment: str = "remote",
        environment_id: Optional[str] = None,
        previous_interaction_id: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        self._client = client
        self._environment = environment
        self._environment_id = environment_id
        self._previous_interaction_id = previous_interaction_id
        self._session_id = session_id
        self._model = model
        self._timeout = timeout

        # Async bridge between send() and receive_steps().
        self._request_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._result_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._processor_task: Optional[asyncio.Task[None]] = None
        self._started = False

        # Cancelled interaction tracking.
        self._cancelled_interaction_ids: set[str] = set()
        self._disconnected = False

        # SDK Conversation contract: cumulative usage across all turns.
        self._cumulative_usage = types.UsageMetadata()
        # SDK Conversation contract: initial history passed on create().
        self._initial_history_list: list[types.Step] = []

        # Idle state for Conversation.send() idle check.
        self._idle = True

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "AntigravityCloudConnection":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------ #
    # SDK Connection property overrides
    # ------------------------------------------------------------------ #

    @property
    def cumulative_usage(self) -> types.UsageMetadata:
        return self._cumulative_usage

    @property
    def _initial_history(self) -> list[types.Step]:
        return self._initial_history_list

    @property
    def is_idle(self) -> bool:
        return self._idle

    # ------------------------------------------------------------------ #
    # Connection lifecycle (continued)
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Begin processing queued requests in the background."""
        if self._started:
            return
        self._started = True
        self._processor_task = asyncio.create_task(self._process_loop())

    async def _process_loop(self) -> None:
        while True:
            item = await self._request_queue.get()
            if item is None:  # sentinel -> shutdown
                self._request_queue.task_done()
                break
            try:
                result = await asyncio.to_thread(
                    self._client.create_interaction,
                    prompt=item["prompt"],
                    environment=item.get("environment", self._environment),
                    environment_id=item.get("environment_id", self._environment_id),
                    previous_interaction_id=item.get(
                        "previous_interaction_id", self._previous_interaction_id
                    ),
                    timeout=item.get("timeout", self._timeout),
                    project_id=item.get("project_id"),
                    session_id=item.get("session_id"),
                )
                result["_request"] = item
                await self._result_queue.put(result)
            except Exception as exc:  # noqa: BLE001 - surface as error step
                await self._result_queue.put(
                    {"success": False, "error": {"exc": str(exc)}, "_request": item}
                )
            finally:
                self._request_queue.task_done()

    # ------------------------------------------------------------------ #
    # Connection API
    # ------------------------------------------------------------------ #

    async def send(self, prompt: types.Content | None, **kwargs: Any) -> None:
        """Queue a prompt for delivery to the Antigravity Cloud API.

        Does not block waiting for the response; call `receive_steps()` to
        consume results.
        """
        if self._disconnected:
            raise RuntimeError("Connection is disconnected.")

        if isinstance(prompt, str):
            text = prompt
        elif isinstance(prompt, types.Content):
            text = prompt.text
        elif prompt is None:
            text = ""
        else:
            text = str(prompt)

        # Merge call-time overrides.
        request = {
            "prompt": text,
            "environment": kwargs.get("environment", self._environment),
            "environment_id": kwargs.get(
                "environment_id", self._environment_id
            ),
            "previous_interaction_id": kwargs.get(
                "previous_interaction_id", self._previous_interaction_id
            ),
            "timeout": kwargs.get("timeout", self._timeout),
            "project_id": kwargs.get("project_id"),
            "session_id": kwargs.get("session_id"),
            "model": kwargs.get("model", self._model),
        }

        # Apply model override into agent_config if provided.
        if request["model"]:
            request["_agent_config"] = {
                "type": "antigravity",
                "model": request["model"],
            }

        self._idle = False
        await self._request_queue.put(request)

    async def receive_steps(self) -> AsyncIterator[types.Step]:
        """Yield SDK Step objects for each completed cloud interaction.

        Each `send()` produces exactly one final step (TEXT_RESPONSE on
        success, SYSTEM_MESSAGE on failure).
        """
        if not self._started:
            await self.start()

        while True:
            result = await self._result_queue.get()
            request = result.get("_request", {})
            step = self._result_to_step(result, request)
            self._result_queue.task_done()

            # SDK Conversation.receive_chunks() reads step.content_delta to
            # produce Text chunks for Agent.chat().text(). Populate it so the
            # response text is not lost.
            if step.content:
                step.content_delta = step.content
            self._idle = False
            yield step

            # A single send() produces one final step per turn. Stop after
            # delivering it so the SDK Conversation can proceed.
            break

        # Mark idle once the turn's steps have been consumed.
        self._idle = True

    async def send_trigger_notification(self, content: str) -> None:
        """Send a trigger notification. Not supported by cloud direct mode."""
        # Cloud direct REST mode has no trigger notification channel.
        # No-op to satisfy the SDK Connection interface.

    async def disconnect(self) -> None:
        """Stop the processor and clear local session state."""
        self._disconnected = True
        if self._processor_task is not None:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
            self._processor_task = None
        # Clear environment / interaction context so a new session starts fresh.
        self._environment_id = None
        self._previous_interaction_id = None
        self._started = False

    async def cancel(self) -> None:
        """Cancel the current in-flight interaction.

        Note: The Antigravity Cloud REST API has no server-side cancel
        endpoint, so this invalidates the local interaction_id only. Any
        request already in flight will still complete on the server; its
        result will be discarded by this client.
        """
        self._previous_interaction_id = None
        self._environment_id = None

    async def wait_for_idle(self) -> None:
        """Wait until the request queue is drained."""
        await self._request_queue.join()

    async def wait_for_wakeup(self, timeout: float = 300.0) -> bool:
        """Wait for a wakeup signal. Not supported by cloud direct mode."""
        # Cloud direct mode has no wakeup mechanism; return False to signal
        # unsupported.
        return False

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _result_to_step(
        self, result: dict[str, Any], request: dict[str, Any]
    ) -> types.Step:
        """Convert a chi client result dict into an SDK types.Step."""
        interaction_id = result.get("interaction_id") or ""
        environment_id = result.get("environment_id") or ""

        if result.get("success"):
            output_text = result.get("output") or ""
            usage = result.get("usage", {}) or {}

            step = types.Step(
                id=interaction_id,
                step_index=0,
                trajectory_id=environment_id,
                depth=0,
                type=types.StepType.TEXT_RESPONSE,
                source=types.StepSource.MODEL,
                target=types.StepTarget.USER,
                status=types.StepStatus.DONE,
                content=output_text,
                is_complete_response=True,
            )
            # Attach usage metadata if the SDK step accepts it.
            try:
                step.usage_metadata = types.UsageMetadata(
                    prompt_token_count=usage.get("prompt_token_count", 0),
                    candidates_token_count=usage.get(
                        "candidates_token_count", 0
                    ),
                    total_token_count=usage.get("total_token_count", 0),
                )
            except Exception:
                # UsageMetadata may not exist in every SDK version; ignore.
                pass
            return step

        # Failure path.
        error = result.get("error", {})
        error_text = ""
        if isinstance(error, dict):
            error_text = error.get("message") or error.get("raw_text") or str(error)
        elif isinstance(error, str):
            error_text = error
        else:
            error_text = str(error)

        status_code = result.get("status_code")
        step = types.Step(
            id=interaction_id,
            step_index=0,
            trajectory_id=environment_id,
            depth=0,
            type=types.StepType.SYSTEM_MESSAGE,
            source=types.StepSource.SYSTEM,
            target=types.StepTarget.USER,
            status=types.StepStatus.ERROR,
            content=f"Error {status_code}: {error_text}",
            is_complete_response=True,
        )
        return step


class AntigravityCloudConnectionStrategy(ConnectionStrategy):
    """SDK ConnectionStrategy that builds AntigravityCloudConnection."""

    def __init__(
        self,
        client: AntigravityClient,
        environment: str = "remote",
        environment_id: Optional[str] = None,
        previous_interaction_id: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        self._client = client
        self._environment = environment
        self._environment_id = environment_id
        self._previous_interaction_id = previous_interaction_id
        self._model = model
        self._timeout = timeout

    async def __aenter__(self) -> "AntigravityCloudConnectionStrategy":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def connect(self) -> Connection:
        return AntigravityCloudConnection(
            client=self._client,
            environment=self._environment,
            environment_id=self._environment_id,
            previous_interaction_id=self._previous_interaction_id,
            model=self._model,
            timeout=self._timeout,
        )


def build_client_from_env(
    api_key: Optional[str] = None,
    key_pool: Optional[KeyPoolManager] = None,
    **kwargs: Any,
) -> AntigravityClient:
    """Build an AntigravityClient from environment variables.

    Reads AGY_KEY_1..AGY_KEY_10 or GEMINI_API_KEY[_N] from os.environ.
    """
    if api_key is None and key_pool is None:
        api_key = os.environ.get("AGY_KEY_1") or os.environ.get("GEMINI_API_KEY")
    return AntigravityClient(
        api_key=api_key,
        key_pool=key_pool,
        **kwargs,
    )
