import os
import io
import unittest
from unittest.mock import MagicMock, patch
import requests

from environment_files import (
    EnvironmentFileClient,
    RemoteFileInfo,
    RemoteManifest,
    PlannedFileAction,
    SyncAction,
    SyncPlan,
    SyncPlanner,
    IntegrityVerifier,
    safe_encode_path,
    DEFAULT_UPLOAD_CHUNK_SIZE
)

class TestPhase73EnvironmentFileAPI(unittest.TestCase):

    def setUp(self):
        self.api_key = "test_api_key"
        self.env_id = "environment-12345"
        self.clean_id = "12345"
        self.client = EnvironmentFileClient(api_key=self.api_key)

    def test_safe_encode_path_valid_and_encoding(self):
        # Valid path encoding
        self.assertEqual(safe_encode_path("foo/bar.txt", "workspace"), "workspace/foo/bar.txt")
        self.assertEqual(safe_encode_path("nested/folder/file#1%20?.txt", "workspace"), "workspace/nested/folder/file%231%2520%3F.txt")

    def test_safe_encode_path_traversal_and_invalid(self):
        # Path traversal and invalid paths
        with self.assertRaises(ValueError):
            safe_encode_path("../secret.txt")
        with self.assertRaises(ValueError):
            safe_encode_path("foo/../../secret.txt")
        with self.assertRaises(ValueError):
            safe_encode_path("/absolute/path")
        with self.assertRaises(ValueError):
            safe_encode_path("foo\x00bar.txt")
        with self.assertRaises(ValueError):
            safe_encode_path("")

    @patch.object(requests.Session, 'get')
    def test_list_files_rest_parameters(self, mock_get):
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {"files": [], "nextPageToken": "token123"}
        mock_get.return_value = mock_res

        res = self.client.list_files("environment-12345", path="workspace", page_size=50, page_token="prev_token")

        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertIn("12345/files", args[0])
        self.assertEqual(kwargs['params']['page_size'], 50)
        self.assertEqual(kwargs['params']['page_token'], "prev_token")
        self.assertEqual(kwargs['params']['recursive'], "true")

    @patch.object(requests.Session, 'get')
    def test_list_all_files_pagination_and_type_normalization(self, mock_get):
        mock_res1 = MagicMock()
        mock_res1.status_code = 200
        mock_res1.json.return_value = {
            "files": [
                {"path": "workspace/f1.txt", "type": "file", "size_bytes": "100"},
                {"path": "workspace/dir1", "type": "directory"}
            ],
            "nextPageToken": "page2"
        }

        mock_res2 = MagicMock()
        mock_res2.status_code = 200
        mock_res2.json.return_value = {
            "files": [
                {"path": "workspace/f2.txt", "type": "FILE", "size_bytes": 200}
            ]
        }

        mock_get.side_effect = [mock_res1, mock_res2]

        files = self.client.list_all_files("environment-12345", base_path="workspace")

        self.assertEqual(len(files), 3)
        self.assertEqual(files[0].type, "FILE")
        self.assertEqual(files[0].size_bytes, 100)
        self.assertEqual(files[1].type, "DIRECTORY")
        self.assertEqual(files[2].type, "FILE")
        self.assertEqual(files[2].size_bytes, 200)

    @patch.object(requests.Session, 'put')
    def test_resumable_upload_success(self, mock_put):
        # 1. Init request response with Location header
        mock_init_res = MagicMock()
        mock_init_res.status_code = 200
        mock_init_res.headers = {"Location": "https://generativelanguage.googleapis.com/upload/session123"}

        # 2. Chunk request response
        mock_chunk_res = MagicMock()
        mock_chunk_res.status_code = 200
        mock_chunk_res.json.return_value = {"name": "workspace/test.txt", "size_bytes": "10"}

        mock_put.side_effect = [mock_init_res, mock_chunk_res]

        content = b"0123456789"
        result = self.client.upload_file("environment-12345", "test.txt", content=content, overwrite=True)

        self.assertEqual(mock_put.call_count, 2)
        # Check init call
        init_call = mock_put.call_args_list[0]
        self.assertEqual(init_call[1]['params']['uploadType'], 'resumable')
        self.assertEqual(init_call[1]['headers']['X-Upload-Content-Length'], '10')

        # Check chunk call
        chunk_call = mock_put.call_args_list[1]
        self.assertEqual(chunk_call[0][0], "https://generativelanguage.googleapis.com/upload/session123")
        self.assertEqual(chunk_call[1]['headers']['Content-Range'], "bytes 0-9/10")
        self.assertEqual(result["name"], "workspace/test.txt")

    @patch.object(requests.Session, 'put')
    def test_resumable_upload_308_handling(self, mock_put):
        mock_init_res = MagicMock()
        mock_init_res.status_code = 200
        mock_init_res.headers = {"Location": "https://upload.session/123"}

        # Chunk 1 returns 308
        mock_chunk1 = MagicMock()
        mock_chunk1.status_code = 308
        mock_chunk1.headers = {"Range": "bytes=0-4"}

        # Chunk 2 returns 200
        mock_chunk2 = MagicMock()
        mock_chunk2.status_code = 200
        mock_chunk2.json.return_value = {"status": "ok"}

        mock_put.side_effect = [mock_init_res, mock_chunk1, mock_chunk2]

        content = b"0123456789"
        result = self.client.upload_file("environment-12345", "large.txt", content=content, chunk_size=5)

        self.assertEqual(mock_put.call_count, 3)
        self.assertEqual(result["status"], "ok")

    @patch.object(requests.Session, 'put')
    def test_upload_409_conflict(self, mock_put):
        mock_init_res = MagicMock()
        mock_init_res.status_code = 409
        mock_init_res.text = "Conflict: File exists"
        mock_put.return_value = mock_init_res

        res = self.client.upload_file("environment-12345", "existing.txt", content=b"data")
        self.assertEqual(res["error"], "CONFLICT")
        self.assertEqual(res["status_code"], 409)

if __name__ == "__main__":
    unittest.main()
