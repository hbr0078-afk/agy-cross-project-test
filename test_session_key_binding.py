import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import requests
from client import AntigravityClient
from keys import KeyPoolManager, AllKeysExhaustedError
from registry import ProjectRegistry

class TestSessionKeyBinding(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.storage_path = os.path.join(self.test_dir.name, "keys", "key_states.json")
        self.registry_path = os.path.join(self.test_dir.name, "registry", "registry.json")
        self.proj_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()
        self.proj_dir.cleanup()

    def _make_manager(self, cooldown_seconds=60):
        return KeyPoolManager(storage_path=self.storage_path, cooldown_seconds=cooldown_seconds)

    def _make_registry(self):
        return ProjectRegistry(storage_path=self.registry_path)

    # Test 1: prefer_key takes precedence when valid
    @patch("requests.post")
    def test_prefer_key_precedence(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_1", "output": "prefer ok"}
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            # Acquire with prefer_key="key2"
            key_ref, key_idx = mgr.acquire_key(prefer_key="key2")
            self.assertEqual(key_ref, "key2")
            self.assertEqual(key_idx, 2)

    # Test 2: Project active_key in registry takes precedence when prefer_key is not specified
    @patch("requests.post")
    def test_project_registry_active_key_precedence(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_1", "output": "registry ok"}
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key2")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            res = client.create_interaction(prompt="test proj affinity")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

    # Test 3: Sticky key persistence across consecutive requests for the same project
    @patch("requests.post")
    def test_sticky_key_maintenance(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_1", "output": "sticky ok"}
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key2")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            
            res1 = client.create_interaction(prompt="req 1")
            self.assertEqual(res1["key_ref"], "key2")

            res2 = client.create_interaction(prompt="req 2")
            self.assertEqual(res2["key_ref"], "key2")

            # Registry active_key remains key2
            p = reg.get_project("proj-A")
            self.assertEqual(p["active_key"], "key2")

    # Test 4: Project isolation (different projects do not interfere with each other's key bindings)
    @patch("requests.post")
    def test_project_isolation(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_1", "output": "isolation ok"}
        mock_post.return_value = mock_resp

        proj_dir_b = tempfile.TemporaryDirectory()
        try:
            env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
            with patch.dict(os.environ, env, clear=True):
                mgr = self._make_manager()
                reg = self._make_registry()
                reg.register_project(self.proj_dir.name, project_id="proj-A")
                reg.register_project(proj_dir_b.name, project_id="proj-B")
                reg.update_project_state("proj-A", active_key="key1")
                reg.update_project_state("proj-B", active_key="key2")

                client_a = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
                client_b = AntigravityClient(key_pool=mgr, project_id="proj-B", registry=reg)

                res_a = client_a.create_interaction(prompt="proj A prompt")
                self.assertEqual(res_a["key_ref"], "key1")

                res_b = client_b.create_interaction(prompt="proj B prompt")
                self.assertEqual(res_b["key_ref"], "key2")
        finally:
            proj_dir_b.cleanup()

    # Test 5: Rollover on 429 rate limit
    @patch("requests.post")
    def test_rollover_on_429(self, mock_post):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {"error": "rate limited"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_success", "output": "rollover success"}

        mock_post.side_effect = [resp_429, resp_200]

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            res = client.create_interaction(prompt="trigger 429 rollover")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

    # Test 6: Registry active_key updated to new key after successful rollover
    @patch("requests.post")
    def test_registry_active_key_updated_after_rollover(self, mock_post):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {"error": "rate limited"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_success", "output": "rollover success"}

        mock_post.side_effect = [resp_429, resp_200]

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            res = client.create_interaction(prompt="rollover update registry")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            # Check registry active_key is updated to key2
            p = reg.get_project("proj-A")
            self.assertEqual(p["active_key"], "key2")

    # Test 7: 401 triggers INACTIVE state and rollover to next key
    @patch("requests.post")
    def test_401_triggers_inactive_and_rollover(self, mock_post):
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.json.return_value = {"error": "invalid auth"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_success", "output": "401 recovered"}

        mock_post.side_effect = [resp_401, resp_200]

        env = {"AGY_KEY_1": "bad-key", "AGY_KEY_2": "good-key"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            res = client.create_interaction(prompt="test 401 rollover")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "INACTIVE")
            self.assertEqual(status["key2"]["state"], "ACTIVE")

            p = reg.get_project("proj-A")
            self.assertEqual(p["active_key"], "key2")

    # Test 8: Sticky key in COOLDOWN is not forced; skips to next available key
    @patch("requests.post")
    def test_sticky_key_in_cooldown_skipped(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_success", "output": "cooldown skip ok"}
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            # Put key1 into COOLDOWN
            mgr.report_result("key1", status_code=429)
            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "COOLDOWN")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            res = client.create_interaction(prompt="cooldown skip prompt")
            self.assertTrue(res["success"])
            # Should skip key1 and use key2
            self.assertEqual(res["key_ref"], "key2")

    # Test 9: Sticky key in INACTIVE is not forced; skips to next available key
    @patch("requests.post")
    def test_sticky_key_in_inactive_skipped(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_success", "output": "inactive skip ok"}
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            # Put key1 into INACTIVE
            mgr.report_result("key1", status_code=401)
            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "INACTIVE")

            client = AntigravityClient(key_pool=mgr, project_id="proj-A", registry=reg)
            res = client.create_interaction(prompt="inactive skip prompt")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

    # Test 10: LRU fallback works when no prefer_key or project active_key is provided
    @patch("requests.post")
    def test_lru_fallback(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_success", "output": "lru ok"}
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            # Use key1 first
            mgr.acquire_key()
            # Next acquisition without prefer or project should pick least recently used (key2)
            ref, idx = mgr.acquire_key()
            self.assertEqual(ref, "key2")

    # Test 11: Concurrent requests for same project maintain binding integrity
    def test_concurrent_same_project_binding(self):
        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            # Simulate multiple rapid acquires with project_id
            for _ in range(10):
                ref, idx = mgr.acquire_key(project_id="proj-A", registry=reg)
                self.assertEqual(ref, "key1")

    # Test 12: Concurrent requests for different projects do not mix bindings
    def test_concurrent_different_projects_binding(self):
        proj_b = tempfile.TemporaryDirectory()
        try:
            env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
            with patch.dict(os.environ, env, clear=True):
                mgr = self._make_manager()
                reg = self._make_registry()
                reg.register_project(self.proj_dir.name, project_id="proj-A")
                reg.register_project(proj_b.name, project_id="proj-B")
                reg.update_project_state("proj-A", active_key="key1")
                reg.update_project_state("proj-B", active_key="key2")

                ref_a, _ = mgr.acquire_key(project_id="proj-A", registry=reg)
                ref_b, _ = mgr.acquire_key(project_id="proj-B", registry=reg)
                self.assertEqual(ref_a, "key1")
                self.assertEqual(ref_b, "key2")
        finally:
            proj_b.cleanup()

    # Test 13: Registry binding persistence across manager restarts
    def test_registry_persistence_across_restart(self):
        reg1 = self._make_registry()
        reg1.register_project(self.proj_dir.name, project_id="proj-A")
        reg1.update_project_state("proj-A", active_key="key2")

        # New registry instance pointing to same file
        reg2 = self._make_registry()
        p = reg2.get_project("proj-A")
        self.assertIsNotNone(p)
        self.assertEqual(p["active_key"], "key2")

    # Test 14: KeyPool state reload maintains COOLDOWN/INACTIVE states
    def test_key_pool_state_persistence(self):
        mgr1 = self._make_manager()
        mgr1.report_result("key1", status_code=429)
        mgr1.report_result("key2", status_code=401)

        mgr2 = self._make_manager()
        status = mgr2.get_status()
        self.assertEqual(status["key1"]["state"], "COOLDOWN")
        self.assertEqual(status["key2"]["state"], "INACTIVE")

    # Test 15: No secret leakage in state, registry, logs or exceptions
    def test_no_secret_leakage_anywhere(self):
        secret = "super-secret-api-key-9999"
        env = {"AGY_KEY_1": secret}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            mgr.acquire_key()  # Ensure state file is created
            reg = self._make_registry()
            reg.register_project(self.proj_dir.name, project_id="proj-A")
            reg.update_project_state("proj-A", active_key="key1")

            # Check state files
            with open(self.storage_path, "r", encoding="utf-8") as f:
                state_content = f.read()
            self.assertNotIn(secret, state_content)

            with open(self.registry_path, "r", encoding="utf-8") as f:
                reg_content = f.read()
            self.assertNotIn(secret, reg_content)


if __name__ == "__main__":
    unittest.main()
