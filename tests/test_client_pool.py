import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import requests
from client import AntigravityClient
from keys import KeyPoolManager, AllKeysExhaustedError

class TestClientKeyPoolRollover(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.storage_path = os.path.join(self.test_dir.name, "keys", "key_states.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def _make_manager(self, cooldown_seconds=60):
        return KeyPoolManager(storage_path=self.storage_path, cooldown_seconds=cooldown_seconds)

    # 1. single-key backward compatibility
    @patch("requests.post")
    def test_single_key_backward_compatibility(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "int_1",
            "environment_id": "env_1",
            "status": "COMPLETED",
            "output": "Hello single",
        }
        mock_post.return_value = mock_resp

        client = AntigravityClient(api_key="fixed-key-123")
        self.assertFalse(client.is_pool_mode)

        res = client.create_interaction(prompt="test prompt")
        self.assertTrue(res["success"])
        self.assertEqual(res["output"], "Hello single")

        call_headers = mock_post.call_args[1]["headers"]
        self.assertEqual(call_headers["x-goog-api-key"], "fixed-key-123")

    # 2. pool mode success
    @patch("requests.post")
    def test_pool_mode_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "int_pool_1",
            "environment_id": "env_pool_1",
            "status": "COMPLETED",
            "output": "Pool success",
        }
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "dummy-key-1"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)
            self.assertTrue(client.is_pool_mode)

            res = client.create_interaction(prompt="test pool")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key1")
            self.assertEqual(res["key_index"], 1)

            # verify state updated on success
            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "ACTIVE")
            self.assertGreater(status["key1"]["last_success_at"], 0)

    # 3. 429 -> key rotation
    @patch("requests.post")
    def test_429_triggers_key_rotation(self, mock_post):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {"error": "Quota exceeded"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_2", "output": "Key2 success"}

        mock_post.side_effect = [resp_429, resp_200]

        env = {"AGY_KEY_1": "key-val-1", "AGY_KEY_2": "key-val-2"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="test 429")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "COOLDOWN")
            self.assertEqual(status["key2"]["state"], "ACTIVE")

    # 4. 401 -> key rotation
    @patch("requests.post")
    def test_401_triggers_key_rotation(self, mock_post):
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.json.return_value = {"error": "API key invalid"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_2", "output": "Key2 success after 401"}

        mock_post.side_effect = [resp_401, resp_200]

        env = {"AGY_KEY_1": "bad-key", "AGY_KEY_2": "good-key"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="test 401")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "INACTIVE")
            self.assertEqual(status["key2"]["state"], "ACTIVE")

    # 5. 403 behavior (record failure via report_result, rotate to next key)
    @patch("requests.post")
    def test_403_rotates_without_immediate_inactive(self, mock_post):
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.json.return_value = {"error": "Permission denied"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_2", "output": "Key2 success"}

        mock_post.side_effect = [resp_403, resp_200]

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="test 403")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            status = mgr.get_status()
            # 1 fail count, not INACTIVE
            self.assertEqual(status["key1"]["state"], "ACTIVE")
            self.assertEqual(status["key1"]["fail_count"], 1)

    # 6. 5xx retry then rotation
    @patch("requests.post")
    def test_5xx_retry_then_rotation(self, mock_post):
        resp_500_1 = MagicMock()
        resp_500_1.status_code = 500
        resp_500_1.json.return_value = {"error": "Internal error"}

        resp_500_2 = MagicMock()
        resp_500_2.status_code = 500
        resp_500_2.json.return_value = {"error": "Internal error retry"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_key2", "output": "Recovered on key2"}

        mock_post.side_effect = [resp_500_1, resp_500_2, resp_200]

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="test 5xx")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            # mock_post should have been called 3 times (key1, key1 retry, key2)
            self.assertEqual(mock_post.call_count, 3)

    # 7. timeout/network retry then rotation
    @patch("requests.post")
    def test_timeout_network_retry_then_rotation(self, mock_post):
        net_err = requests.exceptions.ConnectionError("Connection timed out")
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "int_net_ok", "output": "Net recovered"}

        mock_post.side_effect = [net_err, net_err, resp_200]

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="test net")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")
            self.assertEqual(mock_post.call_count, 3)

    # 8. 400 does not rotate
    @patch("requests.post")
    def test_400_does_not_rotate(self, mock_post):
        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.json.return_value = {"error": "Bad request"}

        mock_post.return_value = resp_400

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="bad prompt")
            self.assertFalse(res["success"])
            self.assertEqual(res["status_code"], 400)
            self.assertEqual(res["key_ref"], "key1")
            self.assertEqual(mock_post.call_count, 1)

            # Key1 is still ACTIVE with 0 fail count
            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "ACTIVE")
            self.assertEqual(status["key1"]["fail_count"], 0)

    # 9. all keys exhausted
    @patch("requests.post")
    def test_all_keys_exhausted_raises_or_returns_failure(self, mock_post):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {"error": "Rate limit"}
        mock_post.return_value = resp_429

        env = {"AGY_KEY_1": "key1-val", "AGY_KEY_2": "key2-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="exhaust test")
            self.assertFalse(res["success"])
            self.assertEqual(res["status_code"], 429)

            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "COOLDOWN")
            self.assertEqual(status["key2"]["state"], "COOLDOWN")

            # Subsequent call raises AllKeysExhaustedError
            with self.assertRaises(AllKeysExhaustedError):
                client.create_interaction(prompt="exhaust test 2")

    # 10. max retry limit prevents infinite loop
    @patch("requests.post")
    def test_max_retry_limit_bounded(self, mock_post):
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.json.return_value = {"error": "Always 500"}
        mock_post.return_value = resp_500

        env = {"AGY_KEY_1": "key1-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)

            res = client.create_interaction(prompt="test 500 loop")
            self.assertFalse(res["success"])
            self.assertLessEqual(mock_post.call_count, 5)

    # 11. key state updated on success
    @patch("requests.post")
    def test_key_state_updated_on_success(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "ok_1", "output": "ok"}
        mock_post.return_value = resp

        env = {"AGY_KEY_1": "key1-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            # pre-set fail_count
            mgr.report_result("key1", status_code=403)
            self.assertEqual(mgr.get_status()["key1"]["fail_count"], 1)

            client = AntigravityClient(key_pool=mgr)
            res = client.create_interaction(prompt="ok prompt")
            self.assertTrue(res["success"])

            status = mgr.get_status()
            self.assertEqual(status["key1"]["fail_count"], 0)
            self.assertEqual(status["key1"]["state"], "ACTIVE")

    # 12. key state updated on failure
    @patch("requests.post")
    def test_key_state_updated_on_failure(self, mock_post):
        resp = MagicMock()
        resp.status_code = 429
        resp.json.return_value = {"error": "rate limited"}
        mock_post.return_value = resp

        env = {"AGY_KEY_1": "key1-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)
            res = client.create_interaction(prompt="test fail")

            self.assertFalse(res["success"])
            status = mgr.get_status()
            self.assertEqual(status["key1"]["state"], "COOLDOWN")

    # 13. previous interaction ID preserved correctly
    @patch("requests.post")
    def test_previous_interaction_id_preserved_correctly(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "int_child", "output": "Child success"}
        mock_post.return_value = resp

        env = {"AGY_KEY_1": "key1-val"}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)
            res = client.create_interaction(
                prompt="next step",
                environment_id="env_123",
                previous_interaction_id="int_parent_999",
            )
            self.assertTrue(res["success"])

            payload = mock_post.call_args[1]["json"]
            self.assertEqual(payload["environment_id"], "env_123")
            self.assertEqual(payload["previous_interaction_id"], "int_parent_999")

    # 14. no credential leakage
    @patch("requests.post")
    def test_no_credential_leakage(self, mock_post):
        secret = "secret-gemini-key-xyz"
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "int_sec", "output": "secure response"}
        mock_post.return_value = resp

        env = {"AGY_KEY_1": secret}
        with patch.dict(os.environ, env, clear=True):
            mgr = self._make_manager()
            client = AntigravityClient(key_pool=mgr)
            res = client.create_interaction(prompt="sec check")

            # Check result dict string
            res_str = str(res)
            self.assertNotIn(secret, res_str)
            self.assertEqual(res["key_ref"], "key1")

            # Check state file
            with open(self.storage_path, "r") as f:
                state_content = f.read()
            self.assertNotIn(secret, state_content)

    # 15. old constructor compatibility
    def test_old_constructor_compatibility(self):
        client = AntigravityClient("old-style-key")
        self.assertEqual(client._api_key, "old-style-key")
        self.assertFalse(client.is_pool_mode)

        with self.assertRaises(ValueError):
            AntigravityClient()


if __name__ == "__main__":
    unittest.main()
