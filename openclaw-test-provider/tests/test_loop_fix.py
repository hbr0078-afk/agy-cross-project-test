#!/usr/bin/env python3
"""Regression tests for the OpenClaw runtime-context infinite-loop fix.

Bug: OpenClaw relocates its runtime-context carrier (a `user` message whose body
is "OpenClaw runtime context: ...") to the TAIL after a tool result
(internal-runtime-context.ts: relocateCurrentRuntimeContextCarrierToTail).
The adapter previously decided "tool result turn" from messages[-1].role == "tool",
so the trailing carrier made every tool-result turn look like a fresh text turn:
the function_result was never submitted, the model re-asked for the same tool,
and the agent looped forever (rotating keys/environments each iteration).

Fix: scan backwards for UNSUBMITTED tool results, tolerating trailing runtime
context carriers.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, "/home/ubuntu/agy-router")
sys.path.insert(0, "/home/ubuntu/agy-router/openclaw-test-provider/adapter")

import adapter
from adapter import _pending_tool_results, _extract_prompt, _pending_function_calls, _SESSIONS

RUNTIME_CTX = (
    "OpenClaw runtime context:\n"
    "Conversation info: \u27e6openclaw:ctx\u27e7\n"
    "```json\n{\"message_id\":\"1\"}\n```\n"
    "End OpenClaw runtime context."
)


def _tool_turn(with_trailing_ctx: bool):
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "exec", "arguments": "{\"command\":\"ls\"}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "exec",
         "content": json.dumps({"output": "a.txt b.txt"})},
    ]
    if with_trailing_ctx:
        msgs.append({"role": "user", "content": RUNTIME_CTX})
    return msgs


class TestPendingToolResults(unittest.TestCase):
    def test_detects_tool_result(self):
        self.assertEqual(len(_pending_tool_results(_tool_turn(False))), 1)

    def test_detects_tool_result_with_trailing_runtime_context(self):
        # This is the exact loop trigger: carrier relocated to tail.
        self.assertEqual(len(_pending_tool_results(_tool_turn(True))), 1)

    def test_real_new_user_turn_is_not_tool_result(self):
        msgs = _tool_turn(True) + [{"role": "user", "content": "do something else"}]
        self.assertEqual(len(_pending_tool_results(msgs)), 0)

    def test_no_assistant_tool_calls_is_not_tool_result(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "c", "content": "x"}]
        self.assertEqual(len(_pending_tool_results(msgs)), 0)


class TestPromptIncludesToolResult(unittest.TestCase):
    def test_tool_output_survives_prompt_build(self):
        p = _extract_prompt(_tool_turn(True))
        self.assertIn("a.txt b.txt", p)

    def test_runtime_context_is_not_leaked_into_prompt(self):
        p = _extract_prompt(_tool_turn(True))
        self.assertNotIn("openclaw:ctx", p)


class TestPendingFunctionCalls(unittest.TestCase):
    """500 regression: a `completed` interaction must NOT emit tool_calls.

    Antigravity returns two shapes:
      - status=requires_action: [function_call]                    -> ask the caller
      - status=completed:       [function_call, function_result,
                                 ..., model_output]                -> already done
    Submitting a function_result for the second shape returns HTTP 500
    ('Internal error encountered.'). Only unanswered function_calls are pending.
    """

    def test_requires_action_call_is_pending(self):
        raw = {"status": "requires_action", "steps": [
            {"type": "function_call", "id": "c1", "name": "exec"}]}
        self.assertEqual(len(_pending_function_calls(raw)), 1)

    def test_completed_call_is_not_pending(self):
        raw = {"status": "completed", "steps": [
            {"type": "function_call", "id": "c1", "name": "exec"},
            {"type": "function_result", "call_id": "c1", "name": "exec"},
            {"type": "model_output", "content": [{"text": "done"}]}]}
        self.assertEqual(_pending_function_calls(raw), [])

    def test_second_unanswered_call_after_answered_one(self):
        raw = {"status": "requires_action", "steps": [
            {"type": "function_call", "id": "c1", "name": "exec"},
            {"type": "function_result", "call_id": "c1", "name": "exec"},
            {"type": "function_call", "id": "c2", "name": "exec"}]}
        pending = _pending_function_calls(raw)
        self.assertEqual([s["id"] for s in pending], ["c2"])


class TestNoInfiniteLoop(unittest.TestCase):
    """Full agent-loop simulation: must converge once the model sees function_result."""

    def setUp(self):
        _SESSIONS.clear()
        self._orig_call = adapter.call_antigravity
        self._orig_multikey = adapter._MULTIKEY
        self._orig_key = adapter.AGY_KEY
        adapter._MULTIKEY = False
        adapter.AGY_KEY = "dummy"

    def tearDown(self):
        adapter.call_antigravity = self._orig_call
        adapter._MULTIKEY = self._orig_multikey
        adapter.AGY_KEY = self._orig_key
        _SESSIONS.clear()

    def test_loop_terminates(self):
        state = {"saw_result": False, "n": 0}

        def fake(api_key, **kw):
            state["n"] += 1
            p = kw.get("prompt")
            if isinstance(p, list) and p and p[0].get("type") == "function_result":
                state["saw_result"] = True
                return {"success": True, "status_code": 200, "environment_id": "envA",
                        "interaction_id": "int-final", "output": "DONE",
                        "raw": {"steps": [{"type": "text", "text": "DONE"}]}}
            return {"success": True, "status_code": 200, "environment_id": "envA",
                    "interaction_id": "int-%d" % state["n"], "output": "",
                    "raw": {"steps": [{"type": "function_call", "id": "gcall_1",
                                       "name": "exec", "arguments": {"command": "ls"}}]}}

        adapter.call_antigravity = fake
        tools = [{"type": "function", "function": {
            "name": "exec", "description": "run",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]

        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "list files"}]
        for i in range(8):
            if i > 0:
                msgs.append({"role": "user", "content": RUNTIME_CTX})
            res = adapter._call(msgs, tools=tools)
            fcall = next((s for s in res.get("raw", {}).get("steps", [])
                          if s.get("type") == "function_call"), None)
            if not fcall:
                break
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "exec", "arguments": json.dumps({"command": "ls"})}}]})
            msgs.append({"role": "tool", "tool_call_id": "c", "tool_name": "exec",
                         "content": json.dumps({"output": "a.txt b.txt"})})
        else:
            self.fail("adapter never converged (infinite loop regression)")

        self.assertTrue(state["saw_result"], "model never received a function_result")


class TestTerminalFunctionResultRecovery(unittest.TestCase):
    """A 500 on a function_result turn must recover as a text turn, not error out."""

    def setUp(self):
        _SESSIONS.clear()
        self._orig_call = adapter.call_antigravity
        self._orig_multikey = adapter._MULTIKEY
        self._orig_key = adapter.AGY_KEY
        adapter._MULTIKEY = False
        adapter.AGY_KEY = "dummy"

    def tearDown(self):
        adapter.call_antigravity = self._orig_call
        adapter._MULTIKEY = self._orig_multikey
        adapter.AGY_KEY = self._orig_key
        _SESSIONS.clear()

    def test_function_result_500_recovers_to_text(self):
        def fake(api_key, **kw):
            p = kw.get("prompt")
            if isinstance(p, list) and p and p[0].get("type") == "function_result":
                return {"success": False, "status_code": 500, "error": "internal"}
            return {"success": True, "status_code": 200, "environment_id": "envA",
                    "interaction_id": "int-final", "output": "RECOVERED",
                    "raw": {"status": "completed", "steps": [
                        {"type": "model_output", "content": [{"text": "RECOVERED"}]}]}}

        adapter.call_antigravity = fake
        tools = [{"type": "function", "function": {
            "name": "exec", "description": "run",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]

        # seed the session as if a tool call just happened
        key = adapter._conv_key([{"role": "user", "content": "go"}])
        _SESSIONS[key] = {"environmentId": "envA", "previousInteractionId": "int-1",
                          "last_call_id": "c1", "last_func_name": "exec",
                          "pin_key_ref": "key1", "fc_streak": 1}

        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "exec", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "tool_name": "exec",
                 "content": json.dumps({"output": "hi"})},
                {"role": "user", "content": RUNTIME_CTX}]

        res = adapter._call(msgs, tools=tools)
        self.assertTrue(res.get("success"), "should recover, not surface 500")
        self.assertEqual(res.get("output"), "RECOVERED")
        self.assertNotIn("pin_key_ref", _SESSIONS[key])


class TestPinnedQuotaReroute(unittest.TestCase):
    """A session is pinned to its environment's owning key (tenant-bound 404 fix).

    If that key later hits its daily quota (429) on a plain text turn, the user
    must NOT be stuck for the rest of the session: the adapter drops the dead
    environment and transparently retries on a fresh key. Context survives
    because the session id is the first user message and the full transcript is
    resent each turn.
    """

    def setUp(self):
        _SESSIONS.clear()
        self._orig_run = adapter._ROLLOVER.run
        self._orig_multikey = adapter._MULTIKEY
        adapter._MULTIKEY = True

    def tearDown(self):
        adapter._ROLLOVER.run = self._orig_run
        adapter._MULTIKEY = self._orig_multikey
        _SESSIONS.clear()

    def test_quota_on_pinned_text_turn_reroutes_to_new_key(self):
        calls = []

        def fake_run(execute, *, tool_bound=False, pinned_key_ref=None, timeout=120):
            calls.append(pinned_key_ref)
            if pinned_key_ref == "key1":
                # pinned owner key is out of quota
                return {"success": False, "status_code": 429,
                        "error": {"message": "Rate limit exceeded"},
                        "key_ref": "key1", "pin_failure": True}
            # free rotation picks a healthy key
            return {"success": True, "status_code": 200, "key_ref": "key5",
                    "environment_id": "envB", "interaction_id": "int-final",
                    "output": "OK-FROM-KEY5",
                    "raw": {"status": "completed", "steps": [
                        {"type": "model_output", "content": [{"text": "OK-FROM-KEY5"}]}]}}

        adapter._ROLLOVER.run = fake_run

        key = adapter._conv_key([{"role": "user", "content": "hello"}])
        _SESSIONS[key] = {"environmentId": "envA", "previousInteractionId": "int-1",
                          "env_key_ref": "key1"}

        res = adapter._call([{"role": "user", "content": "hello"}])
        self.assertTrue(res.get("success"), "must recover, not surface 429")
        self.assertEqual(res.get("output"), "OK-FROM-KEY5")
        # first attempt pinned to the owner key, retry was unpinned (free rotation)
        self.assertEqual(calls[0], "key1")
        self.assertIsNone(calls[-1])
        # session rebinds to the new environment/key
        self.assertEqual(_SESSIONS[key].get("environmentId"), "envB")
        self.assertEqual(_SESSIONS[key].get("env_key_ref"), "key5")


if __name__ == "__main__":
    unittest.main()
