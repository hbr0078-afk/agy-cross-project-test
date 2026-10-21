"""Regression tests for the ported multi-key pool + rollover runner.

Isolated: uses a temp key-state file and fake env keys; never touches
~/.agy-router/key_states.json (shared with chi) nor the network.

Run: python3 -m unittest tests.test_keypool_rollover -v
"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

_ADAPTER_DIR = Path(__file__).resolve().parent.parent / "adapter"
sys.path.insert(0, str(_ADAPTER_DIR))

from key_pool import KeyPoolManager, AllKeysExhaustedError  # noqa: E402
from rollover_runner import RolloverRunner  # noqa: E402


class RolloverTestBase(unittest.TestCase):
    N_KEYS = 3

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in os.environ}
        os.environ["AGY_ADAPTER_DEBUG"] = "1"
        for i in range(1, self.N_KEYS + 1):
            os.environ[f"AGY_KEY_{i}"] = f"TEST_KEY_{i}_SECRET"
        # ensure no stray higher-index env keys leak into discovery
        for i in range(self.N_KEYS + 1, 12):
            os.environ.pop(f"AGY_KEY_{i}", None)
            os.environ.pop(f"GEMINI_API_KEY_{i}", None)
        os.environ.pop("GEMINI_API_KEY", None)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmpdir.name, "key_states.json")
        # block ~/.bashrc fallback so only our env keys are discovered
        self._orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: (os.path.join(self.tmpdir.name, ".bashrc")
                                        if p == "~/.bashrc"
                                        else self._orig_expanduser(p))
        self.pool = KeyPoolManager(storage_path=self.state_path, cooldown_seconds=30)
        self.runner = RolloverRunner(self.pool)

    def tearDown(self):
        os.path.expanduser = self._orig_expanduser
        self.tmpdir.cleanup()
        for k in list(os.environ.keys()):
            if k.startswith(("AGY_KEY_", "GEMINI_API_KEY")) or k == "AGY_ADAPTER_DEBUG":
                if k in self._env_backup and self._env_backup[k] is not None:
                    os.environ[k] = self._env_backup[k]
                else:
                    os.environ.pop(k, None)

    def _state(self):
        with open(self.state_path) as f:
            return json.load(f)["keys"]


class TestKeyPoolState(RolloverTestBase):
    def test_discovery(self):
        self.assertEqual(self.pool.discover_keys(), [1, 2, 3])

    def test_no_raw_secret_in_state_file(self):
        self.pool.acquire_key()
        blob = open(self.state_path).read()
        self.assertIn("key1", blob)
        self.assertNotIn("TEST_KEY_1_SECRET", blob)
        self.assertNotIn("SECRET", blob)

    def test_429_cooldown(self):
        self.pool.acquire_key()
        self.pool.report_result("key1", status_code=429)
        self.assertEqual(self._state()["key1"]["state"], "COOLDOWN")

    def test_401_inactive(self):
        self.pool.acquire_key()
        self.pool.report_result("key1", status_code=401)
        self.assertEqual(self._state()["key1"]["state"], "INACTIVE")

    def test_5xx_threshold_cooldown(self):
        self.pool.acquire_key()
        for _ in range(2):
            self.pool.report_result("key1", status_code=503)
        self.assertEqual(self._state()["key1"]["state"], "ACTIVE")  # below threshold(3)
        self.pool.report_result("key1", status_code=503)
        self.assertEqual(self._state()["key1"]["state"], "COOLDOWN")

    def test_400_no_penalty(self):
        self.pool.acquire_key()
        self.pool.report_result("key1", status_code=400)
        self.assertEqual(self._state()["key1"]["fail_count"], 0)
        self.assertEqual(self._state()["key1"]["state"], "ACTIVE")

    def test_success_resets(self):
        self.pool.acquire_key()
        self.pool.report_result("key1", status_code=503)
        self.pool.report_result("key1", status_code=200)
        self.assertEqual(self._state()["key1"]["fail_count"], 0)


class TestRolloverRunner(RolloverTestBase):
    def test_429_rotates_to_next_key(self):
        seen = []

        def execute(raw_key):
            seen.append(raw_key)
            if raw_key == "TEST_KEY_1_SECRET":
                return {"success": False, "status_code": 429, "error": "rate limited"}
            return {"success": True, "status_code": 200, "output": "ok"}

        res = self.runner.run(execute)
        self.assertTrue(res["success"])
        self.assertEqual(seen[0], "TEST_KEY_1_SECRET")
        self.assertNotEqual(res["key_ref"], "key1")
        self.assertEqual(self._state()["key1"]["state"], "COOLDOWN")

    def test_401_rotates(self):
        def execute(raw_key):
            if raw_key == "TEST_KEY_1_SECRET":
                return {"success": False, "status_code": 401, "error": "auth"}
            return {"success": True, "status_code": 200, "output": "ok"}

        res = self.runner.run(execute)
        self.assertTrue(res["success"])
        self.assertEqual(self._state()["key1"]["state"], "INACTIVE")

    def test_5xx_same_key_retried_once_then_rotate(self):
        calls = []

        def execute(raw_key):
            calls.append(raw_key)
            if raw_key == "TEST_KEY_1_SECRET":
                return {"success": False, "status_code": 500, "error": "server"}
            return {"success": True, "status_code": 200, "output": "ok"}

        res = self.runner.run(execute)
        self.assertTrue(res["success"])
        # key1 hit exactly twice (initial + one same-key retry), then rotated away
        self.assertEqual(calls.count("TEST_KEY_1_SECRET"), 2)

    def test_bounded_attempts_when_all_fail(self):
        calls = []

        def execute(raw_key):
            calls.append(raw_key)
            return {"success": False, "status_code": 429, "error": "limited"}

        res = self.runner.run(execute)
        self.assertFalse(res["success"])
        # max_attempts = max(3*2, 4) = 6; never unbounded
        self.assertLessEqual(len(calls), 6)

    def test_tool_bound_does_not_rotate(self):
        calls = []

        def execute(raw_key):
            calls.append(raw_key)
            return {"success": False, "status_code": 429, "error": "rate limited"}

        res = self.runner.run(execute, tool_bound=True)
        self.assertFalse(res["success"])
        self.assertTrue(res.get("tool_bound_failure"))
        # exactly one call: no rotation attempted on a tool-result turn
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["key_ref"], "key1")

    def test_tool_bound_success_passes_through(self):
        def execute(raw_key):
            return {"success": True, "status_code": 200, "output": "ok"}

        res = self.runner.run(execute, tool_bound=True)
        self.assertTrue(res["success"])
        self.assertNotIn("tool_bound_failure", res)

    def test_400_not_retried(self):
        calls = []

        def execute(raw_key):
            calls.append(raw_key)
            return {"success": False, "status_code": 400, "error": "bad request"}

        res = self.runner.run(execute)
        self.assertFalse(res["success"])
        self.assertEqual(len(calls), 1)  # other 4xx: no retry, no penalty

    def test_exhausted_pool_reports_last_error(self):
        for i in range(1, self.N_KEYS + 1):
            self.pool.report_result(f"key{i}", status_code=401)
        with self.assertRaises(AllKeysExhaustedError):
            self.pool.acquire_key()


class TestPinnedKey(RolloverTestBase):
    """Pinned mode: an active tool chain must stay on ONE key."""

    def test_pin_uses_exact_key_no_rotation(self):
        seen = []

        def execute(raw_key):
            seen.append(raw_key)
            return {"success": True, "status_code": 200, "output": "ok"}

        res = self.runner.run(execute, pinned_key_ref="key2")
        self.assertTrue(res["success"])
        self.assertEqual(seen, ["TEST_KEY_2_SECRET"])  # never key1/key3
        self.assertEqual(res["key_ref"], "key2")

    def test_pin_429_does_not_rotate(self):
        calls = []

        def execute(raw_key):
            calls.append(raw_key)
            return {"success": False, "status_code": 429, "error": "rate limited"}

        res = self.runner.run(execute, pinned_key_ref="key1")
        self.assertFalse(res["success"])
        self.assertTrue(res.get("pin_failure"))
        self.assertEqual(calls, ["TEST_KEY_1_SECRET"])  # no rollover attempt
        self.assertEqual(res["key_ref"], "key1")

    def test_pin_5xx_one_same_key_retry(self):
        calls = []

        def execute(raw_key):
            calls.append(raw_key)
            if len(calls) == 1:
                return {"success": False, "status_code": 503, "error": "server"}
            return {"success": True, "status_code": 200, "output": "ok"}

        res = self.runner.run(execute, pinned_key_ref="key3")
        self.assertTrue(res["success"])
        self.assertEqual(calls, ["TEST_KEY_3_SECRET", "TEST_KEY_3_SECRET"])

    def test_pin_unknown_key_fails_cleanly(self):
        res = self.runner.run(
            lambda k: {"success": True, "status_code": 200}, pinned_key_ref="keyX")
        self.assertFalse(res["success"])
        self.assertTrue(res.get("pin_failure"))
        self.assertEqual(res["error"]["code"], "pin_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
