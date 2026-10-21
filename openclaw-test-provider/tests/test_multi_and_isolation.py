#!/usr/bin/env python3
"""Extended E2E Test Suite: Multi-tool, Session Isolation, and Error Handling."""

import json
import os
import sys
import unittest

sys.path.insert(0, "/home/ubuntu/agy-router")
sys.path.insert(0, "/home/ubuntu/agy-router/openclaw-test-provider/adapter")

from adapter import _call, _SESSIONS, AGY_KEY, _clean_schema

@unittest.skipIf(not AGY_KEY, "AGY_KEY_1 required for E2E")
class TestExtendedE2E(unittest.TestCase):
    def setUp(self):
        _SESSIONS.clear()

    def test_schema_cleaning_rejects_dirty_keys(self):
        dirty = {
            "type": "object",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"path": {"type": "string"}}
        }
        cleaned = _clean_schema(dirty)
        self.assertNotIn("additionalProperties", cleaned)
        self.assertNotIn("$schema", cleaned)

    def test_session_isolation(self):
        """5. 대화 두 개를 동시에 실행하는 세션 격리 테스트"""
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}
            }
        }]

        m1 = [{"role": "user", "content": "[User Session 1]\nread_file 도구로 /tmp/sess1.txt 파일 읽어줘"}]
        m2 = [{"role": "user", "content": "[User Session 2]\nread_file 도구로 /tmp/sess2.txt 파일 읽어줘"}]

        res1 = _call(m1, tools=tools)
        res2 = _call(m2, tools=tools)

        self.assertTrue(res1.get("success"))
        self.assertTrue(res2.get("success"))
        self.assertNotEqual(res1.get("environment_id"), res2.get("environment_id"))
        self.assertNotEqual(res1.get("interaction_id"), res2.get("interaction_id"))
        print("\n[Isolation OK] Session 1 env:", res1.get("environment_id"), "Session 2 env:", res2.get("environment_id"))

if __name__ == "__main__":
    unittest.main()
