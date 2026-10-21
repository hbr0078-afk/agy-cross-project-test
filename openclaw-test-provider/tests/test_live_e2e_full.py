#!/usr/bin/env python3
"""Live E2E Verification Suite for Antigravity Interactions Tool Roundtrip."""

import json
import os
import sys
import unittest

sys.path.insert(0, "/home/ubuntu/agy-router")
sys.path.insert(0, "/home/ubuntu/agy-router/openclaw-test-provider/adapter")

from adapter import _call, _SESSIONS, AGY_KEY

@unittest.skipIf(not AGY_KEY, "AGY_KEY_1 / GEMINI_API_KEY required for Live E2E")
class TestLiveE2EFull(unittest.TestCase):
    def setUp(self):
        _SESSIONS.clear()

    def test_1_normal_conversation(self):
        """1. 일반 대화 회귀 테스트"""
        messages = [{"role": "user", "content": "[User]\n안녕하세요! 자기소개 한 줄 부탁해요."}]
        res = _call(messages)
        self.assertTrue(res.get("success"), f"Failed: {res.get('error')}")
        self.assertEqual(res.get("status_code"), 200)
        self.assertIn("environment_id", res)
        self.assertIn("interaction_id", res)
        print(f"\n[E2E 1 OK] status=200, env_id={res.get('environment_id')}, int_id={res.get('interaction_id')}")

    def test_2_and_3_tool_roundtrip_read_file(self):
        """2 & 3. read 도구 호출 -> function_result -> 최종 답변"""
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read local file content",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "file path"}},
                    "required": ["path"]
                }
            }
        }]

        # Step 1: User prompt
        msg1 = [{"role": "user", "content": "[User]\nread_file 도구를 사용해서 /tmp/test.txt 파일의 내용을 읽어줘."}]
        res1 = _call(msg1, tools=tools)
        self.assertTrue(res1.get("success"), f"Step 1 failed: {res1.get('error')}")
        self.assertEqual(res1.get("status_code"), 200)
        
        raw1 = res1.get("raw", {})
        steps1 = raw1.get("steps", [])
        self.assertTrue(len(steps1) > 0, "No steps in Step 1 response")
        step1 = steps1[0]
        
        real_call_id = step1.get("id")
        func_name = step1.get("name")
        env_id = res1.get("environment_id")
        int_id = res1.get("interaction_id")
        
        print(f"\n[E2E 2 Step 1 OK] status=200, env_id={env_id}, int_id={int_id}, google_call_id={real_call_id}, fn={func_name}")

        # Step 2: OpenClaw sends function_result with dummy call_id
        msg2 = [
            {"role": "user", "content": "[User]\nread_file 도구를 사용해서 /tmp/test.txt 파일의 내용을 읽어줘."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "openclaw_dummy_call_12345",
                    "type": "function",
                    "function": {"name": func_name, "arguments": json.dumps(step1.get("arguments", {}))}
                }]
            },
            {
                "role": "tool",
                "tool_call_id": "openclaw_dummy_call_12345",
                "content": json.dumps({"output": "File Content: HELLO_OPENCLAW_AGY_ROUTER"})
            }
        ]

        res2 = _call(msg2, tools=tools)
        self.assertTrue(res2.get("success"), f"Step 2 failed: {res2.get('error')}")
        self.assertEqual(res2.get("status_code"), 200)
        print(f"[E2E 3 Step 2 OK] status=200, env_id={res2.get('environment_id')}, int_id={res2.get('interaction_id')}")
        print(f"Final Model Reply: {res2.get('output')}")
        self.assertIn("HELLO_OPENCLAW_AGY_ROUTER", res2.get("output", ""))

if __name__ == "__main__":
    unittest.main()
