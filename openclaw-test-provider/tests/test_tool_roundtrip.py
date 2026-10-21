#!/usr/bin/env python3
"""Comprehensive Tool Roundtrip Test Suite for OpenClaw Antigravity Adapter."""

import json
import os
import sys
import unittest

sys.path.insert(0, "/home/ubuntu/agy-router")
sys.path.insert(0, "/home/ubuntu/agy-router/openclaw-test-provider/adapter")

from adapter import _conv_key, _call, _format_tools_for_agy, _SESSIONS

class TestAdapterUnit(unittest.TestCase):
    def setUp(self):
        _SESSIONS.clear()

    def test_schema_cleaning(self):
        from adapter import _clean_schema
        dirty = {
            "type": "object",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {
                "path": {
                    "type": ["string", "null"],
                    "description": "File path",
                    "default": "/tmp/a.txt"
                }
            },
            "required": ["path", "non_existent_key"]
        }
        cleaned = _clean_schema(dirty)
        self.assertNotIn("additionalProperties", cleaned)
        self.assertNotIn("$schema", cleaned)
        self.assertNotIn("default", cleaned)
        self.assertEqual(cleaned["properties"]["path"]["type"], "string")
        self.assertEqual(cleaned["required"], ["path"])

    def test_conv_key_isolation(self):
        m1 = [{"role": "user", "content": "Hello session A"}]
        m2 = [{"role": "user", "content": "Hello session B"}]
        k1 = _conv_key(m1)
        k2 = _conv_key(m2)
        self.assertNotEqual(k1, k2)

if __name__ == "__main__":
    unittest.main()
