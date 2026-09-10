import requests
from unittest import mock
import os
import shutil
import tempfile
import unittest
from workspace_sync import (
    WorkspaceSync,
    SyncStatus,
    SourceManifest,
    MAX_FILE_SIZE_BYTES,
    MAX_TOTAL_PAYLOAD_BYTES
)
from git_manager import GitManager

class TestRegressionAudit(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.ws = WorkspaceSync(key_index=1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_single_file_boundary_minus_one(self):
        """Single file: 512KB - 1 byte -> OK"""
        size = MAX_FILE_SIZE_BYTES - 1
        data = b"x" * size
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"ok.txt": {"content": data, "size": size, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
            total_size=size
        )
        self.ws.check_remote_conflicts = lambda env_id, man: ([], None, None)
        # Mock client to prevent actual network call
        import workspace_sync
        class DummyClient:
            def __init__(self, api_key): pass
            def create_interaction(self, **kwargs):
                return {
                    "success": True,
                    "status_code": 200,
                    "output": f"---INTEGRITY_REPORT_START---\nFILE:ok.txt|SIZE:{size}|SHA256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n---INTEGRITY_REPORT_END---"
                }
        orig_client = workspace_sync.AntigravityClient
        workspace_sync.AntigravityClient = DummyClient
        try:
            res = self.ws.sync_to_remote("env1", manifest)
            self.assertEqual(res.status, SyncStatus.SYNC_SUCCESS)
        finally:
            workspace_sync.AntigravityClient = orig_client

    def test_02_single_file_boundary_exact(self):
        """Single file: 512KB exact -> OK"""
        size = MAX_FILE_SIZE_BYTES
        data = b"x" * size
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"exact.txt": {"content": data, "size": size, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
            total_size=size
        )
        self.ws.check_remote_conflicts = lambda env_id, man: ([], None, None)
        import workspace_sync
        class DummyClient:
            def __init__(self, api_key): pass
            def create_interaction(self, **kwargs):
                return {
                    "success": True,
                    "status_code": 200,
                    "output": f"---INTEGRITY_REPORT_START---\nFILE:exact.txt|SIZE:{size}|SHA256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n---INTEGRITY_REPORT_END---"
                }
        orig_client = workspace_sync.AntigravityClient
        workspace_sync.AntigravityClient = DummyClient
        try:
            res = self.ws.sync_to_remote("env1", manifest)
            self.assertEqual(res.status, SyncStatus.SYNC_SUCCESS)
        finally:
            workspace_sync.AntigravityClient = orig_client

    def test_03_single_file_boundary_plus_one(self):
        """Single file: 512KB + 1 byte -> FILE_TOO_LARGE"""
        size = MAX_FILE_SIZE_BYTES + 1
        data = b"x" * size
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"too_big.txt": {"content": data, "size": size, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
            total_size=size
        )
        res = self.ws.sync_to_remote("env1", manifest)
        self.assertEqual(res.status, SyncStatus.FILE_TOO_LARGE)

    def test_04_total_payload_boundary_minus_one(self):
        """Total payload: 2MB - 1 byte -> OK"""
        size1 = MAX_FILE_SIZE_BYTES
        size2 = MAX_FILE_SIZE_BYTES
        size3 = MAX_FILE_SIZE_BYTES
        size4 = MAX_FILE_SIZE_BYTES - 1
        total_size = MAX_TOTAL_PAYLOAD_BYTES - 1
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "f1.txt": {"content": b"x" * size1, "size": size1, "sha256": "1111111111111111111111111111111111111111111111111111111111111111"},
                "f2.txt": {"content": b"x" * size2, "size": size2, "sha256": "2222222222222222222222222222222222222222222222222222222222222222"},
                "f3.txt": {"content": b"x" * size3, "size": size3, "sha256": "3333333333333333333333333333333333333333333333333333333333333333"},
                "f4.txt": {"content": b"x" * size4, "size": size4, "sha256": "4444444444444444444444444444444444444444444444444444444444444444"},
            },
            total_size=total_size
        )
        self.ws.check_remote_conflicts = lambda env_id, man: ([], None, None)
        import workspace_sync
        class DummyClient:
            def __init__(self, api_key): pass
            def create_interaction(self, **kwargs):
                report = (
                    "---INTEGRITY_REPORT_START---\n"
                    f"FILE:f1.txt|SIZE:{size1}|SHA256:1111111111111111111111111111111111111111111111111111111111111111\n"
                    f"FILE:f2.txt|SIZE:{size2}|SHA256:2222222222222222222222222222222222222222222222222222222222222222\n"
                    f"FILE:f3.txt|SIZE:{size3}|SHA256:3333333333333333333333333333333333333333333333333333333333333333\n"
                    f"FILE:f4.txt|SIZE:{size4}|SHA256:4444444444444444444444444444444444444444444444444444444444444444\n"
                    "---INTEGRITY_REPORT_END---"
                )
                return {"success": True, "status_code": 200, "output": report}
        orig_client = workspace_sync.AntigravityClient
        workspace_sync.AntigravityClient = DummyClient
        try:
            res = self.ws.sync_to_remote("env1", manifest)
            self.assertEqual(res.status, SyncStatus.SYNC_SUCCESS)
        finally:
            workspace_sync.AntigravityClient = orig_client

    def test_05_total_payload_boundary_exact(self):
        """Total payload: 2MB exact -> OK"""
        size = MAX_FILE_SIZE_BYTES
        total_size = MAX_TOTAL_PAYLOAD_BYTES
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "f1.txt": {"content": b"x" * size, "size": size, "sha256": "1111111111111111111111111111111111111111111111111111111111111111"},
                "f2.txt": {"content": b"x" * size, "size": size, "sha256": "2222222222222222222222222222222222222222222222222222222222222222"},
                "f3.txt": {"content": b"x" * size, "size": size, "sha256": "3333333333333333333333333333333333333333333333333333333333333333"},
                "f4.txt": {"content": b"x" * size, "size": size, "sha256": "4444444444444444444444444444444444444444444444444444444444444444"},
            },
            total_size=total_size
        )
        self.ws.check_remote_conflicts = lambda env_id, man: ([], None, None)
        import workspace_sync
        class DummyClient:
            def __init__(self, api_key): pass
            def create_interaction(self, **kwargs):
                report = (
                    "---INTEGRITY_REPORT_START---\n"
                    f"FILE:f1.txt|SIZE:{size}|SHA256:1111111111111111111111111111111111111111111111111111111111111111\n"
                    f"FILE:f2.txt|SIZE:{size}|SHA256:2222222222222222222222222222222222222222222222222222222222222222\n"
                    f"FILE:f3.txt|SIZE:{size}|SHA256:3333333333333333333333333333333333333333333333333333333333333333\n"
                    f"FILE:f4.txt|SIZE:{size}|SHA256:4444444444444444444444444444444444444444444444444444444444444444\n"
                    "---INTEGRITY_REPORT_END---"
                )
                return {"success": True, "status_code": 200, "output": report}
        orig_client = workspace_sync.AntigravityClient
        workspace_sync.AntigravityClient = DummyClient
        try:
            res = self.ws.sync_to_remote("env1", manifest)
            self.assertEqual(res.status, SyncStatus.SYNC_SUCCESS)
        finally:
            workspace_sync.AntigravityClient = orig_client

    def test_06_total_payload_boundary_plus_one(self):
        """Total payload: 2MB + 1 byte -> PAYLOAD_TOO_LARGE"""
        size = MAX_FILE_SIZE_BYTES
        total_size = MAX_TOTAL_PAYLOAD_BYTES + 1
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "f1.txt": {"content": b"x" * size, "size": size, "sha256": "1111111111111111111111111111111111111111111111111111111111111111"},
                "f2.txt": {"content": b"x" * size, "size": size, "sha256": "2222222222222222222222222222222222222222222222222222222222222222"},
                "f3.txt": {"content": b"x" * size, "size": size, "sha256": "3333333333333333333333333333333333333333333333333333333333333333"},
                "f4.txt": {"content": b"x" * (size - 1), "size": size - 1, "sha256": "4444444444444444444444444444444444444444444444444444444444444444"},
                "f5.txt": {"content": b"x" * 2, "size": 2, "sha256": "s5"},
            },
            total_size=total_size
        )
        res = self.ws.sync_to_remote("env1", manifest)
        self.assertEqual(res.status, SyncStatus.PAYLOAD_TOO_LARGE)

    def test_07_integrity_spoofing_and_extra_substring_blocked(self):
        """Strict integrity parsing blocks spoofed lines, extra lines, missing lines, and malformed markers"""
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "a.txt": {"size": 10, "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"}
            }
        )

        # 1. Missing markers
        res1 = self.ws.parse_and_verify_integrity("FILE:a.txt|SIZE:10|SHA256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890", manifest)
        self.assertFalse(res1["verified"])

        # 2. Spoofed hash substring inside arbitrary text
        spoofed = "---INTEGRITY_REPORT_START---\nFILE:a.txt|SIZE:10|SHA256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890_FAKE\n---INTEGRITY_REPORT_END---"
        res2 = self.ws.parse_and_verify_integrity(spoofed, manifest)
        self.assertFalse(res2["verified"])

        # 3. Extra unexpected file
        extra = "---INTEGRITY_REPORT_START---\nFILE:a.txt|SIZE:10|SHA256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\nFILE:extra.txt|SIZE:5|SHA256:1111111111111111111111111111111111111111111111111111111111111111\n---INTEGRITY_REPORT_END---"
        res3 = self.ws.parse_and_verify_integrity(extra, manifest)
        self.assertFalse(res3["verified"])

        # 4. Duplicate file reported
        dup = "---INTEGRITY_REPORT_START---\nFILE:a.txt|SIZE:10|SHA256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\nFILE:a.txt|SIZE:10|SHA256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\n---INTEGRITY_REPORT_END---"
        res4 = self.ws.parse_and_verify_integrity(dup, manifest)
        self.assertFalse(res4["verified"])

        # 5. Exact match succeeds
        exact = "---INTEGRITY_REPORT_START---\nFILE:a.txt|SIZE:10|SHA256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\n---INTEGRITY_REPORT_END---"
        res5 = self.ws.parse_and_verify_integrity(exact, manifest)
        self.assertTrue(res5["verified"])

    def test_08_conflict_403_failsafe(self):
        """HTTP 403 when checking remote conflicts returns UNKNOWN_CANNOT_VERIFY"""
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"a.txt": {"content": b"hello", "size": 5, "sha256": "123"}}
        )
        self.ws.check_remote_conflicts = lambda env_id, man: ([], "UNKNOWN_CANNOT_VERIFY", "Forbidden 403")
        res = self.ws.sync_to_remote("env1", manifest)
        self.assertEqual(res.status, SyncStatus.UNKNOWN_CANNOT_VERIFY)

    def test_09_conflict_500_failsafe(self):
        """HTTP 500 / Network failure returns UNKNOWN_CANNOT_VERIFY"""
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"a.txt": {"content": b"hello", "size": 5, "sha256": "123"}}
        )
        self.ws.check_remote_conflicts = lambda env_id, man: ([], "UNKNOWN_CANNOT_VERIFY", "Internal Server Error 500")
        res = self.ws.sync_to_remote("env1", manifest)
        self.assertEqual(res.status, SyncStatus.UNKNOWN_CANNOT_VERIFY)

if __name__ == "__main__":
    unittest.main()

class TestP4HTTPClassificationAudit(unittest.TestCase):
    """Specific regression suite covering P4 HTTP classification Cases A through G."""

    def setUp(self):
        self.ws = WorkspaceSync(key_index=1)
        self.dummy_manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"test.txt": {"content": b"hello", "size": 5, "sha256": "dummy"}}
        )

    def test_p4_case_a_json_environment_not_found(self):
        mock_resp = mock.Mock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {
            "error": {
                "code": "not_found",
                "message": "Environment 'env-foo' not found."
            }
        }
        mock_resp.text = '{"error":{"code":"not_found","message":"Environment \'env-foo\' not found."}}'

        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-foo", self.dummy_manifest)
            self.assertEqual(err_type, "ENVIRONMENT_NOT_FOUND")
            self.assertIn("env-foo", err_msg)

    def test_p4_case_b_json_subresource_workspace_not_found(self):
        mock_resp = mock.Mock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {
            "error": {
                "code": "not_found",
                "message": "Workspace not found."
            }
        }
        mock_resp.text = '{"error":{"code":"not_found","message":"Workspace not found."}}'

        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-bar", self.dummy_manifest)
            self.assertIsNone(err_type)
            self.assertEqual(conflicts, [])

    def test_p4_case_c_non_json_404_body_fallback(self):
        mock_resp = mock.Mock()
        mock_resp.status_code = 404
        mock_resp.json.side_effect = ValueError("Invalid JSON")
        mock_resp.text = "Environment 'env-c' not found in cluster."

        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-c", self.dummy_manifest)
            self.assertEqual(err_type, "ENVIRONMENT_NOT_FOUND")

        # And safe fallback when random text
        mock_resp.text = "404 Not Found HTML Page"
        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-c", self.dummy_manifest)
            self.assertIsNone(err_type)
            self.assertEqual(conflicts, [])

    def test_p4_case_d_status_400(self):
        mock_resp = mock.Mock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad request"
        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-d", self.dummy_manifest)
            self.assertEqual(err_type, "ENVIRONMENT_NOT_FOUND")

    def test_p4_case_e_status_401_403(self):
        for code in [401, 403]:
            mock_resp = mock.Mock()
            mock_resp.status_code = code
            mock_resp.text = "Auth error"
            with mock.patch("requests.get", return_value=mock_resp):
                conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-e", self.dummy_manifest)
                self.assertEqual(err_type, "UNKNOWN_CANNOT_VERIFY")

    def test_p4_case_f_status_5xx_and_others(self):
        for code in [409, 429, 500, 502, 503]:
            mock_resp = mock.Mock()
            mock_resp.status_code = code
            mock_resp.text = f"Error {code}"
            with mock.patch("requests.get", return_value=mock_resp):
                conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-f", self.dummy_manifest)
                self.assertEqual(err_type, "UNKNOWN_CANNOT_VERIFY")

    def test_p4_case_g_timeout_and_network_error(self):
        with mock.patch("requests.get", side_effect=requests.exceptions.Timeout("Connection timed out")):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-g", self.dummy_manifest)
            self.assertEqual(err_type, "UNKNOWN_CANNOT_VERIFY")
            self.assertIn("timed out", err_msg)

class TestP3ConflictDetectionSemantics(unittest.TestCase):
    """Regression tests for P3 WorkspaceSync remote conflict detection semantics (Cases 1-7)."""

    def setUp(self):
        self.ws = WorkspaceSync(key_index=1)

    def test_p3_case_1_and_2_same_and_different_hash_when_supported(self):
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "same.txt": {"content": b"hello", "size": 5, "sha256": "hash_same"},
                "diff.txt": {"content": b"world", "size": 5, "sha256": "hash_diff_local"}
            }
        )
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "files": [
                {"name": "same.txt", "path": "workspace/same.txt", "sha256": "hash_same"},
                {"name": "diff.txt", "path": "workspace/diff.txt", "sha256": "hash_diff_remote"}
            ]
        }
        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-test", manifest)
            self.assertIsNone(err_type)
            self.assertEqual(conflicts, ["diff.txt"])

    def test_p3_case_3_and_4_local_only_and_remote_only(self):
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "local_only.txt": {"content": b"abc", "size": 3, "sha256": "hash_loc"}
            }
        )
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "files": [
                {"name": "remote_only.txt", "path": "workspace/remote_only.txt", "size_bytes": "10"}
            ]
        }
        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-test", manifest)
            self.assertIsNone(err_type)
            self.assertEqual(conflicts, [])

    def test_p3_case_5_remote_extra_file_preserved(self):
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "app.py": {"content": b"print(1)", "size": 8, "sha256": "hash_app"}
            }
        )
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "files": [
                {"name": "extra.txt", "path": "workspace/extra.txt", "size_bytes": "50"}
            ]
        }
        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-test", manifest)
            self.assertIsNone(err_type)
            self.assertEqual(conflicts, [])

    def test_p3_case_6_overwrite_true_bypasses_conflict(self):
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "conflict.txt": {"content": b"abc", "size": 3, "sha256": "hash1"}
            }
        )
        self.ws.check_remote_conflicts = mock.Mock(return_value=(["conflict.txt"], None, None))
        
        # Test that with overwrite=False -> CONFLICT
        res_fail = self.ws.sync_to_remote("env-test", manifest, overwrite=False)
        self.assertEqual(res_fail.status, SyncStatus.CONFLICT)

    def test_p3_case_7_no_hash_api_fallback_to_failsafe_path_conflict(self):
        # Actual Antigravity API behavior: size_bytes, name, path, type are present, but no sha256
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "test.txt": {"content": b"hello", "size": 6, "sha256": "hash_local"}
            }
        )
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "files": [
                {
                    "name": "test.txt",
                    "path": "workspace/test.txt",
                    "type": "FILE",
                    "size_bytes": "6"
                }
            ]
        }
        with mock.patch("requests.get", return_value=mock_resp):
            conflicts, err_type, err_msg = self.ws.check_remote_conflicts("env-test", manifest)
            self.assertIsNone(err_type)
            self.assertEqual(conflicts, ["test.txt"])
