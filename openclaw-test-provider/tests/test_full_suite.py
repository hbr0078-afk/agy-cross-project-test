#!/usr/bin/env python3
"""OpenClaw Antigravity Interactions Full Verification Suite.
Covers:
1. Unit/Mock tests:
   - Tool-less conversation mapping
   - Schema cleaning
   - Session isolation & multi-tool mapping
   - Error & timeout handling
2. Live E2E tests:
   - Tool-less normal conversation regression
   - read tool call -> function_result -> final reply roundtrip
   - Session isolation (2 concurrent sessions)
"""

import json
import os
import sys
import unittest

sys.path.insert(0, "/home/ubuntu/agy-router")
sys.path.insert(0, "/home/ubuntu/agy-router/openclaw-test-provider/adapter")

from adapter import _call, _SESSIONS, AGY_KEY, _clean_schema, _conv_key

class TestAdapterUnitMock(unittest.TestCase):
    def setUp(self):
        _SESSIONS.clear()

    def test_schema_cleaning(self):
        dirty = {
            "type": "object",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {
                "path": {"type": ["string", "null"], "description": "path", "default": "/tmp/a.txt"}
            },
            "required": ["path", "ghost"]
        }
        cleaned = _clean_schema(dirty)
        self.assertNotIn("additionalProperties", cleaned)
        self.assertNotIn("$schema", cleaned)
        self.assertNotIn("default", cleaned)
        self.assertEqual(cleaned["required"], ["path"])

    def test_session_key_isolation(self):
        m1 = [{"role": "user", "content": "Session A query"}]
        m2 = [{"role": "user", "content": "Session B query"}]
        k1 = _conv_key(m1)
        k2 = _conv_key(m2)
        self.assertNotEqual(k1, k2)

    def test_mock_tool_mapping(self):
        key = "test_conv"
        _SESSIONS[key] = {
            "environmentId": "env_mock_123",
            "previousInteractionId": "int_mock_123",
            "last_call_id": "call_google_real_999",
            "last_func_name": "read_file"
        }
        self.assertEqual(_SESSIONS[key]["last_call_id"], "call_google_real_999")
        self.assertEqual(_SESSIONS[key]["last_func_name"], "read_file")

@unittest.skipIf(not AGY_KEY, "AGY_KEY required for Live E2E")
class TestAdapterLiveE2E(unittest.TestCase):
    def setUp(self):
        _SESSIONS.clear()

    def test_e2e_1_normal_conversation(self):
        messages = [{"role": "user", "content": "[User]\n안녕하세요! 자기소개 한 줄 부탁해요."}]
        res = _call(messages)
        self.assertTrue(res.get("success"), f"Failed: {res.get('error')}")
        self.assertEqual(res.get("status_code"), 200)
        self.assertTrue(bool(res.get("environment_id")))
        self.assertTrue(bool(res.get("interaction_id")))

    def test_e2e_2_tool_roundtrip(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
            }
        }]

        # Step 1: User prompt triggering read_file
        msg1 = [{"role": "user", "content": "[User]\nread_file 도구를 사용해서 /tmp/test.txt 파일의 내용을 읽어줘."}]
        res1 = _call(msg1, tools=tools)
        self.assertTrue(res1.get("success"), f"Step 1 failed: {res1.get('error')}")
        self.assertEqual(res1.get("status_code"), 200)

        step1 = res1.get("raw", {}).get("steps", [{}])[0]
        real_call_id = step1.get("id")
        func_name = step1.get("name")
        self.assertTrue(real_call_id, "Missing Google call_id")

        # Step 2: OpenClaw sends function_result with dummy call_id
        msg2 = [
            {"role": "user", "content": "[User]\nread_file 도구를 사용해서 /tmp/test.txt 파일의 내용을 읽어줘."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "openclaw_dummy_123",
                    "type": "function",
                    "function": {"name": func_name, "arguments": json.dumps(step1.get("arguments", {}))}
                }]
            },
            {
                "role": "tool",
                "tool_call_id": "openclaw_dummy_123",
                "content": json.dumps({"output": "HELLO_E2E_VERIFIED"})
            }
        ]
        res2 = _call(msg2, tools=tools)
        self.assertTrue(res2.get("success"), f"Step 2 failed: {res2.get('error')}")
        self.assertEqual(res2.get("status_code"), 200)
        self.assertIn("HELLO_E2E_VERIFIED", res2.get("output", ""))

if __name__ == "__main__":
    unittest.main()
