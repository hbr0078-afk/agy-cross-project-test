import os
import io
import shutil
import tarfile
import tempfile
import unittest
from snapshot_manager import SnapshotManager, SnapshotResult, SnapshotInspectResult, SnapshotRestoreResult
from client import AntigravityClient
from keys import get_api_key_by_index

class TestSnapshotManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.sm = SnapshotManager(key_index=1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_dummy_tar(self, files_dict, filename="test.tar"):
        tar_path = os.path.join(self.test_dir, filename)
        with tarfile.open(tar_path, "w") as tar:
            for name, content in files_dict.items():
                if isinstance(content, str):
                    content_bytes = content.encode('utf-8')
                else:
                    content_bytes = content
                ti = tarfile.TarInfo(name=name)
                ti.size = len(content_bytes)
                tar.addfile(ti, io.BytesIO(content_bytes))
        return tar_path

    def test_01_snapshot_download_invalid_key(self):
        # Test 1: Download fails gracefully on invalid key index
        bad_sm = SnapshotManager(key_index=99)
        res = bad_sm.download_snapshot("dummy_id", os.path.join(self.test_dir, "snap.tar"))
        self.assertFalse(res.success)
        self.assertIn("No API key available", res.error_message)

    def test_02_snapshot_inspect(self):
        # Test 2: Inspect tar archive
        tar_path = self._create_dummy_tar({
            "workspace/file1.txt": "Hello World",
            "workspace/file2.txt": "Antigravity Router"
        })

        inspect = self.sm.inspect_snapshot(tar_path)
        self.assertTrue(inspect.is_valid)
        self.assertEqual(inspect.file_count, 2)
        self.assertTrue(inspect.has_workspace)
        self.assertIn("workspace/file1.txt", inspect.file_list)
        self.assertGreater(inspect.size_bytes, 0)
        self.assertEqual(len(inspect.sha256), 64)

    def test_03_snapshot_restore(self):
        # Test 3: Restore to temporary destination
        tar_path = self._create_dummy_tar({
            "workspace/hello.py": "print('hello')",
            "README.md": "# Readme"
        })

        dest = os.path.join(self.test_dir, "restored")
        res = self.sm.restore_snapshot(tar_path, dest)
        self.assertTrue(res.success)
        self.assertTrue(os.path.exists(os.path.join(dest, "workspace", "hello.py")))
        self.assertTrue(os.path.exists(os.path.join(dest, "README.md")))

        with open(os.path.join(dest, "workspace", "hello.py"), "r") as f:
            self.assertEqual(f.read(), "print('hello')")

    def test_04_invalid_archive(self):
        # Test 4: Plain text file treated as snapshot fails cleanly
        plain_file = os.path.join(self.test_dir, "invalid.tar")
        with open(plain_file, "w") as f:
            f.write("This is plain text, not a tar archive.")

        inspect = self.sm.inspect_snapshot(plain_file)
        self.assertFalse(inspect.is_valid)
        self.assertIn("not a valid tar archive", inspect.error_message)

        dest = os.path.join(self.test_dir, "restored_invalid")
        res = self.sm.restore_snapshot(plain_file, dest)
        self.assertFalse(res.success)

    def test_05_corrupt_tar_archive(self):
        # Test 5: Corrupt tar archive handling
        corrupt_file = os.path.join(self.test_dir, "corrupt.tar")
        with open(corrupt_file, "wb") as f:
            f.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00corrupt data header here")

        inspect = self.sm.inspect_snapshot(corrupt_file)
        self.assertFalse(inspect.is_valid)

        dest = os.path.join(self.test_dir, "restored_corrupt")
        res = self.sm.restore_snapshot(corrupt_file, dest)
        self.assertFalse(res.success)

    def test_06_path_traversal_relative(self):
        # Test 6: Path traversal (../../evil.txt) blocked safely
        tar_path = os.path.join(self.test_dir, "traversal_rel.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti = tarfile.TarInfo(name="../../evil.txt")
            data = b"malicious content"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        dest = os.path.join(self.test_dir, "safe_restore_dir")
        res = self.sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertIn("INVALID_ARCHIVE_PATH", res.error_message)
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "evil.txt")))

    def test_07_path_traversal_absolute(self):
        # Test 7: Absolute path (/etc/evil.txt) blocked safely
        tar_path = os.path.join(self.test_dir, "traversal_abs.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti = tarfile.TarInfo(name="/etc/evil.txt")
            data = b"malicious content"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        dest = os.path.join(self.test_dir, "safe_restore_dir_abs")
        res = self.sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertIn("INVALID_ARCHIVE_PATH", res.error_message)

    def test_08_destination_isolation(self):
        # Test 8: Restore isolated inside destination directory
        outside_file = os.path.join(self.test_dir, "outside_unaffected.txt")
        with open(outside_file, "w") as f:
            f.write("ORIGINAL_CONTENT")

        tar_path = self._create_dummy_tar({"file.txt": "NEW_CONTENT"})
        dest = os.path.join(self.test_dir, "iso_dest")
        res = self.sm.restore_snapshot(tar_path, dest)

        self.assertTrue(res.success)
        with open(outside_file, "r") as f:
            self.assertEqual(f.read(), "ORIGINAL_CONTENT")

    def test_09_secret_leakage_masking(self):
        # Test 9: Secret masking in snapshot errors
        key = get_api_key_by_index(1)
        if key:
            err_text = f"Failed download with key {key} at https://x-access-token:{key}@github.com"
            from snapshot_manager import mask_credentials
            masked = mask_credentials(err_text)
            self.assertNotIn(key, masked)

    def test_10_real_e2e_snapshot_cycle(self):
        # Test 10: Real Antigravity remote environment interaction -> download -> inspect -> restore -> verify marker
        key = get_api_key_by_index(1)
        if not key:
            self.skipTest("No API key available for real E2E test")

        client = AntigravityClient(api_key=key)
        marker_str = "REMOTE_SNAPSHOT_TEST_OK"
        prompt = f"Please write strictly '{marker_str}' into /workspace/SNAPSHOT_MARKER.txt and output DONE."

        res = client.create_interaction(prompt=prompt, environment="remote")
        self.assertTrue(res.get("success"), f"Interaction failed: {res.get('error')}")

        env_id = res.get("environment_id")
        self.assertIsNotNone(env_id)

        # Download snapshot
        snap_path = os.path.join(self.test_dir, "e2e_snapshot.tar")
        dl_res = self.sm.download_snapshot(env_id, snap_path)
        self.assertTrue(dl_res.success, f"Download failed: {dl_res.error_message}")
        self.assertTrue(os.path.exists(snap_path))

        # Inspect snapshot
        insp_res = self.sm.inspect_snapshot(snap_path)
        self.assertTrue(insp_res.is_valid)
        self.assertTrue(insp_res.has_workspace)

        # Restore snapshot
        restore_dir = os.path.join(self.test_dir, "e2e_restore")
        rst_res = self.sm.restore_snapshot(snap_path, restore_dir)
        self.assertTrue(rst_res.success, f"Restore failed: {rst_res.error_message}")

        # Verify marker content
        marker_file = os.path.join(restore_dir, "workspace", "SNAPSHOT_MARKER.txt")
        self.assertTrue(os.path.exists(marker_file), "SNAPSHOT_MARKER.txt not found in restored archive")
        with open(marker_file, "r") as f:
            content = f.read().strip()
            self.assertIn(marker_str, content)

if __name__ == "__main__":
    unittest.main()
