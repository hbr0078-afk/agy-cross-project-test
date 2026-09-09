import os
import io
import shutil
import tempfile
import tarfile
import subprocess
import unittest
from snapshot_manager import SnapshotManager, SnapshotResult, SnapshotRestoreResult
from git_manager import GitManager, GitState, GitOpStatus
from workspace_sync import (
    WorkspaceSync,
    SyncStatus,
    SourceManifest,
    MAX_FILE_SIZE_BYTES,
    MAX_TOTAL_PAYLOAD_BYTES
)
from registry import ProjectRegistry, RegistryCorruptedError
from keys import get_api_key_by_index

class TestSecurityAndReliabilityRevisions(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agy_sec_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # 1. SnapshotManager: Symlink / Hardlink Security Tests
    # -------------------------------------------------------------
    def test_sec_01_symlink_to_etc_passwd(self):
        """Security: Symlink pointing to /etc/passwd must be rejected."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_etc")
        tar_path = os.path.join(self.test_dir, "symlink_etc.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti = tarfile.TarInfo(name="symlink_etc")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)

        res = sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertIn("INVALID_ARCHIVE_SYMLINK", res.error_message)
        self.assertFalse(os.path.exists(os.path.join(dest, "symlink_etc")))

    def test_sec_02_symlink_outside_destination(self):
        """Security: Relative symlink pointing outside destination must be rejected."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_out")
        outside_target = os.path.join(self.test_dir, "outside.txt")
        with open(outside_target, "w") as f:
            f.write("sensitive_host_info")

        tar_path = os.path.join(self.test_dir, "symlink_outside.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti = tarfile.TarInfo(name="symlink_out")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../outside.txt"
            tar.addfile(ti)

        res = sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertIn("INVALID_ARCHIVE_SYMLINK", res.error_message)

    def test_sec_03_symlink_pivot_write_outside(self):
        """Security: Creating a symlink dir then extracting files through it must be blocked and write prevented."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_pivot")
        victim_dir = os.path.join(self.test_dir, "victim_dir")
        os.makedirs(victim_dir, exist_ok=True)
        victim_file = os.path.join(victim_dir, "pwned.txt")

        tar_path = os.path.join(self.test_dir, "pivot.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti1 = tarfile.TarInfo(name="linkdir")
            ti1.type = tarfile.SYMTYPE
            ti1.linkname = victim_dir
            tar.addfile(ti1)

            data = b"PWNED_PAYLOAD"
            ti2 = tarfile.TarInfo(name="linkdir/pwned.txt")
            ti2.size = len(data)
            tar.addfile(ti2, io.BytesIO(data))

        res = sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertFalse(os.path.exists(victim_file), "Victim file outside dest must NOT be created!")

    def test_sec_04_hardlink_outside_destination(self):
        """Security: Hardlink pointing to file outside destination must be blocked."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_hardlink")
        host_target = os.path.join(self.test_dir, "host_secret.txt")
        with open(host_target, "w") as f:
            f.write("secret")

        tar_path = os.path.join(self.test_dir, "hardlink_out.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti = tarfile.TarInfo(name="hardlink_entry")
            ti.type = tarfile.LNKTYPE
            ti.linkname = host_target
            tar.addfile(ti)

        res = sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertIn("INVALID_ARCHIVE_HARDLINK", res.error_message)

    def test_sec_05_nested_symlinks(self):
        """Security: Nested symlinks pointing outside must be caught."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_nested")
        tar_path = os.path.join(self.test_dir, "nested_symlink.tar")
        with tarfile.open(tar_path, "w") as tar:
            ti1 = tarfile.TarInfo(name="sub/link1")
            ti1.type = tarfile.SYMTYPE
            ti1.linkname = "../../outside"
            tar.addfile(ti1)

        res = sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertIn("INVALID_ARCHIVE_SYMLINK", res.error_message)

    def test_sec_06_safe_internal_symlink(self):
        """Security: Normal relative symlink strictly within destination is safely restored."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_safe_sym")
        tar_path = os.path.join(self.test_dir, "safe_symlink.tar")
        with tarfile.open(tar_path, "w") as tar:
            data = b"REAL_FILE_CONTENT"
            ti1 = tarfile.TarInfo(name="workspace/real.txt")
            ti1.size = len(data)
            tar.addfile(ti1, io.BytesIO(data))

            ti2 = tarfile.TarInfo(name="workspace/sym.txt")
            ti2.type = tarfile.SYMTYPE
            ti2.linkname = "real.txt" # safe relative sibling
            tar.addfile(ti2)

        res = sm.restore_snapshot(tar_path, dest)
        self.assertTrue(res.success, f"Safe relative symlink should be allowed: {res.error_message}")
        real_p = os.path.join(dest, "workspace", "real.txt")
        sym_p = os.path.join(dest, "workspace", "sym.txt")
        self.assertTrue(os.path.exists(real_p))
        self.assertTrue(os.path.islink(sym_p))

    def test_sec_07_restore_no_partial_leak_on_failure(self):
        """Atomicity: On validation failure, destination must remain clean without partial files."""
        sm = SnapshotManager()
        dest = os.path.join(self.test_dir, "dest_atomic")
        tar_path = os.path.join(self.test_dir, "atomic_leak_test.tar")
        with tarfile.open(tar_path, "w") as tar:
            # 1. Normal file
            data = b"HELLO"
            ti1 = tarfile.TarInfo(name="first_file.txt")
            ti1.size = len(data)
            tar.addfile(ti1, io.BytesIO(data))

            # 2. Poison entry
            ti2 = tarfile.TarInfo(name="bad_symlink")
            ti2.type = tarfile.SYMTYPE
            ti2.linkname = "/etc/shadow"
            tar.addfile(ti2)

        res = sm.restore_snapshot(tar_path, dest)
        self.assertFalse(res.success)
        self.assertFalse(os.path.exists(os.path.join(dest, "first_file.txt")), "Partial files must NOT be left on failure!")

    # -------------------------------------------------------------
    # 2. WorkspaceSync: Integrity & Verification Tests
    # -------------------------------------------------------------
    def test_verify_08_sha256_match_pass(self):
        """Integrity: Identical file path, size, and SHA256 pass verification."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "app.py": {
                    "size": 15,
                    "sha256": "54b20fc6abdd83313bc4924c538cb9861ec3f13a1fb4d01f8087265a6b7d5267",
                    "content": b"print('hello')\n",
                    "is_binary": False
                }
            }
        )
        report = (
            "---INTEGRITY_REPORT_START---\n"
            "FILE:app.py|SIZE:15|SHA256:54b20fc6abdd83313bc4924c538cb9861ec3f13a1fb4d01f8087265a6b7d5267\n"
            "---INTEGRITY_REPORT_END---\n"
            "WORKSPACE_SYNC_COMPLETE"
        )
        res = ws.parse_and_verify_integrity(report, manifest)
        self.assertTrue(res["verified"])
        self.assertIn("app.py", res["verified_files"])

    def test_verify_09_content_mismatch_sha256_fail(self):
        """Integrity: Same name & size, but modified content (different SHA256) fails."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "app.py": {
                    "size": 15,
                    "sha256": "54b20fc6abdd83313bc4924c538cb9861ec3f13a1fb4d01f8087265a6b7d5267",
                    "content": b"print('hello')\n",
                    "is_binary": False
                }
            }
        )
        report = (
            "---INTEGRITY_REPORT_START---\n"
            "FILE:app.py|SIZE:15|SHA256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "---INTEGRITY_REPORT_END---\n"
            "WORKSPACE_SYNC_COMPLETE"
        )
        res = ws.parse_and_verify_integrity(report, manifest)
        self.assertFalse(res["verified"])
        self.assertIn("SHA256 mismatch", res["error_message"])

    def test_verify_10_size_mismatch_fail(self):
        """Integrity: Size mismatch fails verification."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "app.py": {
                    "size": 15,
                    "sha256": "54b20fc6abdd83313bc4924c538cb9861ec3f13a1fb4d01f8087265a6b7d5267",
                    "content": b"print('hello')\n",
                    "is_binary": False
                }
            }
        )
        report = (
            "---INTEGRITY_REPORT_START---\n"
            "FILE:app.py|SIZE:99|SHA256:54b20fc6abdd83313bc4924c538cb9861ec3f13a1fb4d01f8087265a6b7d5267\n"
            "---INTEGRITY_REPORT_END---\n"
            "WORKSPACE_SYNC_COMPLETE"
        )
        res = ws.parse_and_verify_integrity(report, manifest)
        self.assertFalse(res["verified"])
        self.assertIn("Size mismatch", res["error_message"])

    def test_verify_11_missing_file_fail(self):
        """Integrity: Manifest file missing from report fails."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "app.py": {"size": 10, "sha256": "abc", "content": b"x", "is_binary": False},
                "missing.py": {"size": 20, "sha256": "def", "content": b"y", "is_binary": False}
            }
        )
        report = (
            "---INTEGRITY_REPORT_START---\n"
            "FILE:app.py|SIZE:10|SHA256:abc\n"
            "---INTEGRITY_REPORT_END---\n"
        )
        res = ws.parse_and_verify_integrity(report, manifest)
        self.assertFalse(res["verified"])
        self.assertIn("missing.py", res["error_message"])

    def test_verify_12_malformed_remote_response_fail(self):
        """Integrity: Unparseable / corrupted remote response fails."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"app.py": {"size": 10, "sha256": "abc", "content": b"x", "is_binary": False}}
        )
        res = ws.parse_and_verify_integrity("Internal Server Error or gibberish", manifest)
        self.assertFalse(res["verified"])

    # -------------------------------------------------------------
    # 3. GitManager: User Staged Isolation Tests
    # -------------------------------------------------------------
    def test_git_13_isolated_checkpoint_preserves_user_staged(self):
        """Git: Router checkpoint on specified file must NOT include user's pre-staged secret file."""
        repo_dir = os.path.join(self.test_dir, "repo_iso")
        os.makedirs(repo_dir)
        subprocess.run(["git", "-C", repo_dir, "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "config", "user.name", "test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@test.com"], check=True, capture_output=True)

        app_py = os.path.join(repo_dir, "app.py")
        with open(app_py, "w") as f: f.write("print('v1')\n")
        subprocess.run(["git", "-C", repo_dir, "add", "app.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "commit", "-m", "init"], check=True, capture_output=True)

        # 1. User pre-stages secret.txt
        secret_txt = os.path.join(repo_dir, "secret.txt")
        with open(secret_txt, "w") as f: f.write("SUPER_SECRET_TOKEN\n")
        subprocess.run(["git", "-C", repo_dir, "add", "secret.txt"], check=True, capture_output=True)

        # 2. Router modifies app.py and checkpoints app.py ONLY
        with open(app_py, "w") as f: f.write("print('v2')\n")

        gm = GitManager(repo_dir)
        res = gm.commit_checkpoint("checkpoint app.py only", files=["app.py"])
        self.assertEqual(res.status, GitOpStatus.COMMITTED)

        # 3. Verify commit contains app.py ONLY
        show_files = subprocess.run(
            ["git", "-C", repo_dir, "show", "--name-only", "--oneline", res.commit_sha],
            capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("app.py", show_files)
        self.assertNotIn("secret.txt", show_files, "secret.txt MUST NOT be included in checkpoint commit!")

        # 4. Verify user's secret.txt remains staged in git status
        st = subprocess.run(["git", "-C", repo_dir, "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertIn("A  secret.txt", st, "User staged secret.txt must remain staged in index!")

    # -------------------------------------------------------------
    # 4. WorkspaceSync: Binary and Size Limits Tests
    # -------------------------------------------------------------
    def test_binary_14_detection_and_base64_handling(self):
        """Binary: Binary files are detected and handled with Base64 encoding in manifest."""
        ws = WorkspaceSync()
        src_dir = os.path.join(self.test_dir, "bin_src")
        os.makedirs(src_dir)
        bin_p = os.path.join(src_dir, "image.png")
        with open(bin_p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01")

        manifest, err = ws.prepare_source_from_local(src_dir)
        self.assertIsNone(err)
        self.assertTrue(manifest.files["image.png"]["is_binary"])

    def test_size_15_single_file_too_large(self):
        """Size Limit: Exceeding single file limit is blocked before interaction."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "giant.bin": {
                    "size": MAX_FILE_SIZE_BYTES + 1024,
                    "sha256": "abc",
                    "content": b"0" * (MAX_FILE_SIZE_BYTES + 1024),
                    "is_binary": True
                }
            },
            total_size=MAX_FILE_SIZE_BYTES + 1024
        )
        res = ws.sync_to_remote("test_env", manifest)
        self.assertEqual(res.status, SyncStatus.FILE_TOO_LARGE)

    def test_size_16_total_payload_too_large(self):
        """Size Limit: Exceeding total payload limit is blocked before interaction."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={},
            total_size=MAX_TOTAL_PAYLOAD_BYTES + 1024
        )
        res = ws.sync_to_remote("test_env", manifest)
        self.assertEqual(res.status, SyncStatus.PAYLOAD_TOO_LARGE)

    # -------------------------------------------------------------
    # 5. WorkspaceSync: Fail-Safe Conflict Detection
    # -------------------------------------------------------------
    def test_conflict_17_unknown_cannot_verify_fail_safe(self):
        """Conflict Fail-safe: When remote listing returns 403 or error, returns UNKNOWN_CANNOT_VERIFY without blind overwrite."""
        ws = WorkspaceSync()
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"test.txt": {"size": 4, "sha256": "123", "content": b"test", "is_binary": False}}
        )
        # Mock conflict check returning error
        ws.check_remote_conflicts = lambda env_id, man: ([], "UNKNOWN_CANNOT_VERIFY", "API endpoint error 500")

        res = ws.sync_to_remote("test_env", manifest, overwrite=False)
        self.assertEqual(res.status, SyncStatus.UNKNOWN_CANNOT_VERIFY)

    # -------------------------------------------------------------
    # 6. Registry: Concurrency (fcntl) and Permission Tests
    # -------------------------------------------------------------
    def test_reg_18_permissions_and_locking(self):
        """Registry: Storage dir and file have 0700/0600 permissions."""
        reg_dir = os.path.join(self.test_dir, "reg_secure_dir")
        reg_file = os.path.join(reg_dir, "registry.json")
        reg = ProjectRegistry(storage_path=reg_file)

        reg.register_project(self.test_dir, project_id="sec-proj")
        self.assertTrue(os.path.exists(reg_file))

        # Check permissions
        dir_mode = oct(os.stat(reg_dir).st_mode & 0o777)
        file_mode = oct(os.stat(reg_file).st_mode & 0o777)
        self.assertEqual(dir_mode, "0o700")
        self.assertEqual(file_mode, "0o600")

if __name__ == "__main__":
    unittest.main()
