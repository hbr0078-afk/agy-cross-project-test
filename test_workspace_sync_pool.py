import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from workspace_sync import WorkspaceSync, SyncStatus, SourceManifest, mask_credentials
from keys import KeyPoolManager, AllKeysExhaustedError, get_api_key_by_index
import requests

class TestWorkspaceSyncPool(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.test_dir, "key_states.json")
        self.pool = KeyPoolManager(storage_path=self.state_file, cooldown_seconds=60)
        self.manifest = SourceManifest(
            source_type="local",
            source_path=self.test_dir,
            files={
                "test.txt": {
                    "full_path": os.path.join(self.test_dir, "test.txt"),
                    "size": 4,
                    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                    "content": b"test",
                    "is_binary": False
                }
            },
            total_size=4
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_mock_keys(self, count=2):
        keys_dict = {}
        for i in range(1, count + 1):
            keys_dict[f"key{i}"] = {
                "index": i,
                "state": "ACTIVE",
                "fail_count": 0,
                "last_status_code": None,
                "cooldown_until": None,
                "last_used_at": 0,
                "last_success_at": 0,
            }
        self.pool._save_raw({"keys": keys_dict})

    # 1. KeyPool 정상 sync
    @patch("workspace_sync.AntigravityClient")
    @patch.object(WorkspaceSync, "check_remote_conflicts")
    def test_01_keypool_normal_sync(self, mock_conflicts, mock_client_cls):
        self._setup_mock_keys(2)
        mock_conflicts.return_value = ([], None, None)
        
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.create_interaction.return_value = {
            "success": True,
            "status_code": 200,
            "output": (
                "---INTEGRITY_REPORT_START---\n"
                "FILE:test.txt|SIZE:4|SHA256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n"
                "---INTEGRITY_REPORT_END---\n"
                "WORKSPACE_SYNC_COMPLETE"
            )
        }

        ws = WorkspaceSync(key_pool=self.pool)
        self.assertTrue(ws.is_pool_mode)
        res = ws.sync_to_remote("env_1", self.manifest)
        self.assertEqual(res.status, SyncStatus.SYNC_SUCCESS)
        self.assertIn("test.txt", res.verified_files)

    # 2. 429 -> key rotation -> 성공
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_02_conflict_check_429_rotation_success(self, mock_get_key, mock_get):
        self._setup_mock_keys(2)
        mock_get_key.side_effect = lambda idx: f"mock_key_{idx}"

        # First request on key1 returns 429, second on key2 returns 200
        resp_429 = MagicMock(status_code=429, text="Rate limit")
        resp_429.json.return_value = {"error": {"message": "RESOURCE_EXHAUSTED"}}
        
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"files": []}

        mock_get.side_effect = [resp_429, resp_200]

        ws = WorkspaceSync(key_pool=self.pool)
        conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", self.manifest)

        self.assertEqual(conflicts, [])
        self.assertIsNone(err_type)
        self.assertIsNone(err_msg)

        status = self.pool.get_status()
        self.assertEqual(status["key1"]["state"], "COOLDOWN")
        self.assertEqual(status["key2"]["state"], "ACTIVE")

    # 3. 401 -> key inactive -> 다음 key 성공
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_03_conflict_check_401_inactive_next_key_success(self, mock_get_key, mock_get):
        self._setup_mock_keys(2)
        mock_get_key.side_effect = lambda idx: f"mock_key_{idx}"

        resp_401 = MagicMock(status_code=401, text="Unauthorized")
        resp_401.json.return_value = {"error": {"message": "Invalid API Key"}}

        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"files": []}

        mock_get.side_effect = [resp_401, resp_200]

        ws = WorkspaceSync(key_pool=self.pool)
        conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", self.manifest)

        self.assertEqual(conflicts, [])
        self.assertIsNone(err_type)

        status = self.pool.get_status()
        self.assertEqual(status["key1"]["state"], "INACTIVE")
        self.assertEqual(status["key2"]["state"], "ACTIVE")

    # 4. 5xx -> retry/rotation -> 성공
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_04_conflict_check_5xx_retry_and_rotation(self, mock_get_key, mock_get):
        self._setup_mock_keys(2)
        mock_get_key.side_effect = lambda idx: f"mock_key_{idx}"

        resp_500 = MagicMock(status_code=500, text="Internal Server Error")
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"files": []}

        # key1 1st: 500 -> retry same key once -> key1 2nd: 500 -> rotate to key2 -> key2: 200
        mock_get.side_effect = [resp_500, resp_500, resp_200]

        ws = WorkspaceSync(key_pool=self.pool)
        conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", self.manifest)

        self.assertEqual(conflicts, [])
        self.assertIsNone(err_type)

        # 3 calls made (key1 retry once, then key2)
        self.assertEqual(mock_get.call_count, 3)

    # 5. 400/404 -> rotation하지 않음
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_05_conflict_check_404_no_rotation(self, mock_get_key, mock_get):
        self._setup_mock_keys(2)
        mock_get_key.side_effect = lambda idx: f"mock_key_{idx}"

        resp_404 = MagicMock(status_code=404, text="Workspace not found")
        resp_404.json.return_value = {"error": {"message": "workspace not found"}}
        mock_get.return_value = resp_404

        ws = WorkspaceSync(key_pool=self.pool)
        conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", self.manifest)

        # Should NOT rotate: exactly 1 get call made
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(conflicts, [])
        self.assertIsNone(err_type)

    # 6. AllKeysExhaustedError
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_06_all_keys_exhausted_error(self, mock_get_key, mock_get):
        self._setup_mock_keys(2)
        mock_get_key.side_effect = lambda idx: f"mock_key_{idx}"

        resp_401 = MagicMock(status_code=401, text="Unauthorized")
        resp_401.json.return_value = {"error": {"message": "Invalid API Key"}}
        mock_get.return_value = resp_401

        ws = WorkspaceSync(key_pool=self.pool)
        with self.assertRaises(AllKeysExhaustedError):
            ws.check_remote_conflicts("env_1", self.manifest)

        status = self.pool.get_status()
        self.assertEqual(status["key1"]["state"], "INACTIVE")
        self.assertEqual(status["key2"]["state"], "INACTIVE")

    # 7. conflict check에서도 rotation 정상 동작
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_07_conflict_check_with_conflicts_after_rotation(self, mock_get_key, mock_get):
        self._setup_mock_keys(2)
        mock_get_key.side_effect = lambda idx: f"mock_key_{idx}"

        resp_429 = MagicMock(status_code=429, text="Rate limit")
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "files": [
                {"name": "test.txt", "size_bytes": 4, "checksum": "different_sha"}
            ]
        }
        mock_get.side_effect = [resp_429, resp_200]

        ws = WorkspaceSync(key_pool=self.pool)
        conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", self.manifest)
        self.assertEqual(conflicts, ["test.txt"])

    # 8. sync에서도 rotation 정상 동작
    @patch.object(WorkspaceSync, "check_remote_conflicts")
    def test_08_sync_rotation_integration(self, mock_conflicts):
        self._setup_mock_keys(2)
        mock_conflicts.return_value = ([], None, None)

        ws = WorkspaceSync(key_pool=self.pool)

        with patch("client.requests.post") as mock_post, patch("client.get_api_key_by_index") as mock_get_key:
            mock_get_key.side_effect = lambda idx: f"mock_api_key_{idx}"
            
            resp_429 = MagicMock(status_code=429, text="Rate limited")
            resp_429.json.return_value = {"error": {"message": "Quota exceeded"}}
            
            resp_200 = MagicMock(status_code=200)
            resp_200.json.return_value = {
                "output": (
                    "---INTEGRITY_REPORT_START---\n"
                    "FILE:test.txt|SIZE:4|SHA256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08\n"
                    "---INTEGRITY_REPORT_END---\n"
                    "WORKSPACE_SYNC_COMPLETE"
                )
            }
            mock_post.side_effect = [resp_429, resp_200]

            res = ws.sync_to_remote("env_1", self.manifest)
            self.assertEqual(res.status, SyncStatus.SYNC_SUCCESS)
            self.assertEqual(mock_post.call_count, 2)
            
            pool_status = self.pool.get_status()
            self.assertEqual(pool_status["key1"]["state"], "COOLDOWN")
            self.assertEqual(pool_status["key2"]["state"], "ACTIVE")

    # 9. 기존 단일 key_index 모드 regression
    @patch("requests.get")
    @patch("keys.get_api_key_by_index")
    def test_09_legacy_single_key_mode(self, mock_get_key, mock_get):
        mock_get_key.return_value = "mock_single_key"
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"files": []}
        mock_get.return_value = resp_200

        ws = WorkspaceSync(key_index=1)
        self.assertFalse(ws.is_pool_mode)
        conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", self.manifest)
        self.assertEqual(conflicts, [])
        self.assertEqual(mock_get.call_count, 1)

    # 10. API key 원문 로그/예외 미노출
    def test_10_api_key_masked_in_logs_and_exceptions(self):
        raw_key = "AIzaSyD_fake_key_for_testing_12345"
        text_with_key = f"Error occurred using key {raw_key} at endpoint."
        masked = mask_credentials(text_with_key)
        self.assertNotIn(raw_key, masked)
        self.assertIn("***", masked)

    # 11. 동시성 상태 무결성
    def test_11_concurrent_state_integrity(self):
        import concurrent.futures
        self._setup_mock_keys(3)

        def worker(w_id):
            pool = KeyPoolManager(storage_path=self.state_file)
            for _ in range(5):
                try:
                    ref, idx = pool.acquire_key()
                    pool.report_result(ref, status_code=200)
                except AllKeysExhaustedError:
                    pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(4)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        status = self.pool.get_status()
        self.assertEqual(len(status), 3)
        for k, v in status.items():
            self.assertEqual(v["state"], "ACTIVE")

if __name__ == "__main__":
    unittest.main()
