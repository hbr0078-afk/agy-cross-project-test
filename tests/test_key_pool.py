import os
import json
import stat
import time
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ProcessPoolExecutor

import keys
from keys import (
    KeyPoolManager,
    AllKeysExhaustedError,
    KeyPoolCorruptedError,
    get_api_key_by_index,
)

def _worker_acquire(storage_path: str):
    """Standalone top-level helper for multiprocessing concurrency test."""
    manager = KeyPoolManager(storage_path=storage_path, cooldown_seconds=60)
    key_ref, idx = manager.acquire_key()
    return key_ref, idx

class TestKeyPool(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.storage_dir = os.path.join(self.test_dir.name, "agy-keys")
        self.state_path = os.path.join(self.storage_dir, "key_states.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def _make_manager(self, cooldown_seconds=60):
        return KeyPoolManager(storage_path=self.state_path, cooldown_seconds=cooldown_seconds)

    # 1. single-key backward compatibility
    def test_single_key_backward_compatibility(self):
        with patch.dict(os.environ, {"AGY_KEY_1": "dummy-secret-agy-1", "GEMINI_API_KEY": "dummy-fallback-1"}, clear=True):
            key = get_api_key_by_index(1)
            self.assertEqual(key, "dummy-secret-agy-1")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "dummy-gemini-only"}, clear=True):
            key = get_api_key_by_index(1)
            self.assertEqual(key, "dummy-gemini-only")

    # 2. 2-key discovery
    def test_two_key_discovery(self):
        env = {
            "AGY_KEY_1": "dummy-key-1",
            "AGY_KEY_2": "dummy-key-2",
        }
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            discovered = mgr.discover_keys()
            self.assertEqual(discovered, [1, 2])

    # 3. non-contiguous key discovery (1, 3, 5)
    def test_non_contiguous_key_discovery(self):
        env = {
            "AGY_KEY_1": "dummy-1",
            "GEMINI_API_KEY_3": "dummy-3",
            "AGY_KEY_5": "dummy-5",
        }
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            discovered = mgr.discover_keys()
            self.assertEqual(discovered, [1, 3, 5])

    # 4. ACTIVE key acquisition
    def test_active_key_acquisition(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            ref, idx = mgr.acquire_key()
            self.assertEqual(ref, "key1")
            self.assertEqual(idx, 1)

    # 5. LRU selection
    def test_lru_selection(self):
        env = {
            "AGY_KEY_1": "dummy-1",
            "AGY_KEY_2": "dummy-2",
            "AGY_KEY_3": "dummy-3",
        }
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            # Initial order of acquisition should cycle through least recently used
            ref1, _ = mgr.acquire_key()
            time.sleep(0.01)
            ref2, _ = mgr.acquire_key()
            time.sleep(0.01)
            ref3, _ = mgr.acquire_key()
            time.sleep(0.01)

            self.assertEqual({ref1, ref2, ref3}, {"key1", "key2", "key3"})

            # Now, ref1 was used earliest, so next acquire must be ref1
            next_ref, _ = mgr.acquire_key()
            self.assertEqual(next_ref, ref1)

    # 6. prefer_key
    def test_prefer_key(self):
        env = {
            "AGY_KEY_1": "dummy-1",
            "AGY_KEY_2": "dummy-2",
        }
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            ref, idx = mgr.acquire_key(prefer_key="key2")
            self.assertEqual(ref, "key2")
            self.assertEqual(idx, 2)

    # 7. cooldown 만료 후 자동 ACTIVE 복귀
    def test_cooldown_expiry_recovery(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=1)
            mgr.acquire_key()
            mgr.report_result("key1", status_code=429)

            # Immediately exhausted
            with self.assertRaises(AllKeysExhaustedError):
                mgr.acquire_key()

            # Wait for cooldown expiration
            time.sleep(1.1)

            # Automatically recovers to ACTIVE
            ref, idx = mgr.acquire_key()
            self.assertEqual(ref, "key1")
            self.assertEqual(idx, 1)

    # 8. 429 -> COOLDOWN
    def test_status_429_triggers_cooldown(self):
        env = {"AGY_KEY_1": "dummy-1", "AGY_KEY_2": "dummy-2"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=60)
            mgr.acquire_key()
            entry = mgr.report_result("key1", status_code=429)
            self.assertEqual(entry["state"], "COOLDOWN")
            self.assertIsNotNone(entry["cooldown_until"])

            # Next acquire must pick key2
            ref, idx = mgr.acquire_key()
            self.assertEqual(ref, "key2")

    # 9. 401 -> INACTIVE
    def test_status_401_triggers_inactive(self):
        env = {"AGY_KEY_1": "dummy-1", "AGY_KEY_2": "dummy-2"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=1)
            entry = mgr.report_result("key1", status_code=401)
            self.assertEqual(entry["state"], "INACTIVE")
            self.assertIsNone(entry["cooldown_until"])

            # Even after waiting, key1 remains INACTIVE
            time.sleep(1.1)
            ref, idx = mgr.acquire_key()
            self.assertEqual(ref, "key2")

    # 10. 403 1회 -> INACTIVE 아님
    def test_status_403_single_does_not_deactivate(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            entry = mgr.report_result("key1", status_code=403)
            self.assertEqual(entry["state"], "ACTIVE")
            self.assertEqual(entry["fail_count"], 1)

            # Key is still acquirable
            ref, idx = mgr.acquire_key()
            self.assertEqual(ref, "key1")

    # 11. 403 3회 -> COOLDOWN
    def test_status_403_three_times_triggers_cooldown(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=60)
            mgr.report_result("key1", status_code=403)
            mgr.report_result("key1", status_code=403)
            entry = mgr.report_result("key1", status_code=403)
            self.assertEqual(entry["state"], "COOLDOWN")
            self.assertEqual(entry["fail_count"], 3)

            with self.assertRaises(AllKeysExhaustedError):
                mgr.acquire_key()

    # 12. 5xx 3회 -> COOLDOWN
    def test_status_5xx_three_times_triggers_cooldown(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=60)
            mgr.report_result("key1", status_code=500)
            mgr.report_result("key1", status_code=502)
            entry = mgr.report_result("key1", status_code=503)
            self.assertEqual(entry["state"], "COOLDOWN")
            self.assertEqual(entry["fail_count"], 3)

            with self.assertRaises(AllKeysExhaustedError):
                mgr.acquire_key()

    # 13. timeout/network 3회 -> COOLDOWN
    def test_timeout_network_three_times_triggers_cooldown(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=60)
            mgr.report_result("key1", status_code=None)
            mgr.report_result("key1", status_code=0)
            entry = mgr.report_result("key1", status_code=None)
            self.assertEqual(entry["state"], "COOLDOWN")
            self.assertEqual(entry["fail_count"], 3)

            with self.assertRaises(AllKeysExhaustedError):
                mgr.acquire_key()

    # 14. 400 -> key penalty 없음
    def test_status_400_no_penalty(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            entry = mgr.report_result("key1", status_code=400)
            self.assertEqual(entry["state"], "ACTIVE")
            self.assertEqual(entry["fail_count"], 0)

            ref, _ = mgr.acquire_key()
            self.assertEqual(ref, "key1")

    # 15. all keys exhausted -> AllKeysExhaustedError
    def test_all_keys_exhausted_raises(self):
        env = {"AGY_KEY_1": "dummy-1", "AGY_KEY_2": "dummy-2"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=60)
            mgr.report_result("key1", status_code=401)
            mgr.report_result("key2", status_code=429)

            with self.assertRaises(AllKeysExhaustedError):
                mgr.acquire_key()

    # 16. state persistence
    def test_state_persistence(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr1 = self._make_manager(cooldown_seconds=60)
            mgr1.acquire_key()
            mgr1.report_result("key1", status_code=429)

            # Create a brand new instance pointing to same file
            mgr2 = self._make_manager(cooldown_seconds=60)
            status = mgr2.get_status()
            self.assertEqual(status["key1"]["state"], "COOLDOWN")
            self.assertEqual(status["key1"]["fail_count"], 1)

    # 17. corrupted state -> KeyPoolCorruptedError
    def test_corrupted_state_raises(self):
        os.makedirs(self.storage_dir, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json corrupted content")

        mgr = self._make_manager()
        with self.assertRaises(KeyPoolCorruptedError):
            mgr.get_status()

    # 18. concurrent acquisition / file lock
    def test_concurrent_acquisition_multiprocess(self):
        env = {
            "AGY_KEY_1": "dummy-1",
            "AGY_KEY_2": "dummy-2",
            "AGY_KEY_3": "dummy-3",
        }
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            mgr.get_status()  # initialize file

            with ProcessPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(_worker_acquire, self.state_path) for _ in range(12)]
                results = [f.result() for f in futures]

            self.assertEqual(len(results), 12)
            # Verify no corruption occurred and valid keys were selected
            for key_ref, idx in results:
                self.assertIn(key_ref, ["key1", "key2", "key3"])
                self.assertIn(idx, [1, 2, 3])

            status = mgr.get_status()
            self.assertEqual(len(status), 3)

    # 19. reset_key
    def test_reset_key(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager(cooldown_seconds=60)
            mgr.report_result("key1", status_code=401)
            self.assertEqual(mgr.get_status()["key1"]["state"], "INACTIVE")

            ok = mgr.reset_key("key1")
            self.assertTrue(ok)
            entry = mgr.get_status()["key1"]
            self.assertEqual(entry["state"], "ACTIVE")
            self.assertEqual(entry["fail_count"], 0)
            self.assertIsNone(entry["cooldown_until"])

    # 20. 실제 API key 문자열이 state/log/test output에 노출되지 않음
    def test_no_secret_leak_in_state_file(self):
        secret_value = "super-secret-unmasked-token-99999"
        env = {"AGY_KEY_1": secret_value}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            mgr.acquire_key()
            mgr.report_result("key1", status_code=200)

            with open(self.state_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertNotIn(secret_value, content)
            self.assertIn("key1", content)
            self.assertIn("ACTIVE", content)

    # 21. state file 0600
    def test_state_file_permissions(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            mgr.acquire_key()

            file_mode = stat.S_IMODE(os.stat(self.state_path).st_mode)
            self.assertEqual(file_mode, 0o600)

    # 22. state directory 0700
    def test_state_directory_permissions(self):
        env = {"AGY_KEY_1": "dummy-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            mgr.acquire_key()

            dir_mode = stat.S_IMODE(os.stat(self.storage_dir).st_mode)
            self.assertEqual(dir_mode, 0o700)

if __name__ == "__main__":
    unittest.main()
