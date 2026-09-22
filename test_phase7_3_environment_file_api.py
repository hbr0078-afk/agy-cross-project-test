import os
import unittest
from unittest.mock import patch, MagicMock
from environment_files import (
    EnvironmentFileClient, SyncPlanner, SyncAction,
    IntegrityVerifier, RemoteManifest, RemoteFileInfo, PlannedFileAction
)
from workspace_sync import WorkspaceSync, SyncStatus, SourceManifest

class TestPhase7_3EnvironmentFileAPI(unittest.TestCase):
    def setUp(self):
        self.client = EnvironmentFileClient(api_key="test_key")

    @patch("requests.Session.get")
    def test_01_list_files_root(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "files": [
                {"path": "workspace/a.txt", "type": "FILE", "size_bytes": "10"}
            ]
        }
        mock_get.return_value = mock_resp

        res = self.client.list_files("env123", path="workspace")
        self.assertIn("files", res)
        self.assertEqual(len(res["files"]), 1)

    @patch("requests.Session.get")
    def test_02_pagination_complete(self, mock_get):
        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.json.return_value = {
            "files": [{"path": "workspace/1.txt", "type": "FILE"}],
            "nextPageToken": "tok2"
        }
        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {
            "files": [{"path": "workspace/2.txt", "type": "FILE"}]
        }
        mock_get.side_effect = [mock_resp1, mock_resp2]

        all_files = self.client.list_all_files("env123")
        self.assertEqual(len(all_files), 2)
        self.assertEqual(all_files[0].path, "workspace/1.txt")
        self.assertEqual(all_files[1].path, "workspace/2.txt")

    @patch("requests.Session.get")
    def test_03_download_file_alt_media(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"content_data"
        mock_get.return_value = mock_resp

        data = self.client.download_file("env123", "workspace/a.txt")
        self.assertEqual(data, b"content_data")
        # Verify params
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs.get("params"), {"alt": "media"})

    @patch("requests.Session.put")
    def test_04_upload_file_overwrite_false_409(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "Entity exists"
        mock_put.return_value = mock_resp

        res = self.client.upload_file("env123", "workspace/a.txt", b"data", overwrite=False)
        self.assertEqual(res.get("error"), "CONFLICT")
        self.assertEqual(res.get("status_code"), 409)

    def test_05_sync_planner_diff(self):
        source = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "new.txt": {"size": 10, "sha256": "aaa"},
                "same.txt": {"size": 20, "sha256": "bbb"},
                "diff.txt": {"size": 30, "sha256": "ccc"}
            }
        )
        remote_files = [
            RemoteFileInfo(path="workspace/same.txt", type="FILE", size_bytes=20, sha256="bbb"),
            RemoteFileInfo(path="workspace/diff.txt", type="FILE", size_bytes=30, sha256="diff_sha"),
            RemoteFileInfo(path="workspace/remote_only.txt", type="FILE", size_bytes=50, sha256="eee")
        ]
        remote_manifest = SyncPlanner.build_remote_manifest("env123", remote_files, base_dir="workspace")
        
        # Test overwrite=False
        plan = SyncPlanner.plan(source, remote_manifest, overwrite=False)
        self.assertTrue(plan.has_conflicts)
        self.assertIn("diff.txt", plan.conflicts)

        # Test overwrite=True
        plan_ow = SyncPlanner.plan(source, remote_manifest, overwrite=True)
        self.assertFalse(plan_ow.has_conflicts)
        
        actions = {a.rel_path: a.action for a in plan_ow.actions}
        self.assertEqual(actions["new.txt"], SyncAction.UPLOAD)
        self.assertEqual(actions["same.txt"], SyncAction.UNCHANGED)
        self.assertEqual(actions["diff.txt"], SyncAction.UPDATE)
        self.assertEqual(actions["remote_only.txt"], SyncAction.REMOTE_ONLY)

    def test_06_path_traversal_rejection(self):
        ws = WorkspaceSync(key_index=1)
        src = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"../evil.py": {"size": 10, "sha256": "abc", "content": b"x"}}
        )
        res = ws.sync_to_remote("env123", src)
        self.assertEqual(res.status, SyncStatus.INVALID_PATH)

if __name__ == "__main__":
    unittest.main()
