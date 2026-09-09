import os
import shutil
import tempfile
import unittest
from workspace_sync import WorkspaceSync, SyncStatus, SourceManifest, mask_credentials
from git_manager import GitManager
from client import AntigravityClient
from keys import get_api_key_by_index

class TestWorkspaceSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.ws = WorkspaceSync(key_index=1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_local_source_prepare(self):
        # Test 1: Normal local source preparation
        src_dir = os.path.join(self.test_dir, "local_src")
        os.makedirs(os.path.join(src_dir, "subdir"), exist_ok=True)
        with open(os.path.join(src_dir, "main.py"), "w") as f:
            f.write("print('hello')")
        with open(os.path.join(src_dir, "subdir", "config.json"), "w") as f:
            f.write('{"env": "test"}')

        manifest, err = self.ws.prepare_source_from_local(src_dir)
        self.assertIsNone(err)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.source_type, "local")
        self.assertEqual(len(manifest.files), 2)
        self.assertIn("main.py", manifest.files)
        self.assertIn(os.path.join("subdir", "config.json"), manifest.files)

    def test_02_git_source_prepare(self):
        # Test 2: Git commit source preparation
        repo_dir = os.path.join(self.test_dir, "git_repo")
        os.makedirs(repo_dir, exist_ok=True)

        gm = GitManager(repo_dir)
        gm._run_git(["init", "-b", "main"])
        gm._run_git(["config", "user.name", "Test Runner"])
        gm._run_git(["config", "user.email", "test@example.com"])

        f1 = os.path.join(repo_dir, "ROLLOVER_TEST.txt")
        with open(f1, "w") as f:
            f.write("KEY1_COMPLETED\n")
        gm._run_git(["add", "ROLLOVER_TEST.txt"])
        gm._run_git(["commit", "-m", "Initial commit"])

        status = gm.get_status()
        target_sha = status.head_commit

        manifest, err = self.ws.prepare_source_from_git(repo_dir, commit_sha=target_sha)
        self.assertIsNone(err)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.source_type, "git")
        self.assertEqual(manifest.commit_sha, target_sha)
        self.assertIn("ROLLOVER_TEST.txt", manifest.files)

    def test_03_source_not_found(self):
        # Test 3: Non-existent local source path
        bad_dir = os.path.join(self.test_dir, "non_existent_folder")
        manifest, err = self.ws.prepare_source_from_local(bad_dir)
        self.assertIsNone(manifest)
        self.assertIn("does not exist", err)

    def test_04_invalid_environment(self):
        # Test 4: Transfer to invalid/non-existent environment
        src_dir = os.path.join(self.test_dir, "valid_src")
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, "test.txt"), "w") as f:
            f.write("data")

        manifest, _ = self.ws.prepare_source_from_local(src_dir)
        res = self.ws.sync_to_remote("invalid_env_id_12345", manifest)
        self.assertIn(res.status, [SyncStatus.ENVIRONMENT_NOT_FOUND, SyncStatus.TRANSFER_FAILED, SyncStatus.ERROR])

    def test_05_path_traversal_detection(self):
        # Test 5: Path traversal attempt blocked safely
        src_dir = os.path.join(self.test_dir, "traversal_src")
        os.makedirs(src_dir, exist_ok=True)
        
        manifest = SourceManifest(
            source_type="local",
            source_path=src_dir,
            files={
                "../../evil.txt": {"content": b"malicious", "size": 9, "sha256": "123"}
            }
        )

        res = self.ws.sync_to_remote("valid_env", manifest)
        self.assertEqual(res.status, SyncStatus.INVALID_PATH)

    def test_06_conflict_detection_fail_safe(self):
        # Test 6: Conflict detected and blocked when overwrite=False
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"existing_file.txt": {"content": b"data", "size": 4, "sha256": "123"}}
        )

        self.ws.check_remote_conflicts = lambda env_id, man: (["existing_file.txt"], None)

        res = self.ws.sync_to_remote("test_env", manifest, overwrite=False)
        self.assertEqual(res.status, SyncStatus.CONFLICT)
        self.assertIn("existing_file.txt", res.skipped_files)

    def test_07_transfer_failure(self):
        # Test 7: Simulated transfer interaction error
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={"file.txt": {"content": b"data", "size": 4, "sha256": "123"}}
        )

        self.ws.check_remote_conflicts = lambda env_id, man: ([], None)
        
        ws_bad = WorkspaceSync(key_index=99)
        res = ws_bad.sync_to_remote("test_env", manifest, overwrite=True)
        self.assertEqual(res.status, SyncStatus.ERROR)

    def test_08_verification_failure(self):
        # Test 8: Verification failure handling
        manifest = SourceManifest(
            source_type="local",
            source_path="/dummy",
            files={
                "file1.txt": {"content": b"data1", "size": 5, "sha256": "111"},
                "file2.txt": {"content": b"data2", "size": 5, "sha256": "222"}
            }
        )

        self.ws.check_remote_conflicts = lambda env_id, man: ([], None)
        self.ws.verify_remote = lambda env_id, man: {
            "verified": False,
            "verified_files": ["file1.txt"],
            "error_message": "VERIFY_FAILED: Missing file2.txt"
        }

        import workspace_sync
        class DummyClient:
            def __init__(self, api_key): pass
            def create_interaction(self, **kwargs):
                return {"success": True, "status_code": 200, "output": "WORKSPACE_SYNC_COMPLETE"}

        orig_client = workspace_sync.AntigravityClient
        workspace_sync.AntigravityClient = DummyClient
        try:
            res = self.ws.sync_to_remote("test_env", manifest, overwrite=True)
            self.assertEqual(res.status, SyncStatus.VERIFY_FAILED)
        finally:
            workspace_sync.AntigravityClient = orig_client

    def test_09_secret_leakage_masking(self):
        # Test 9: Secret masking in logs/outputs
        key = get_api_key_by_index(1)
        if key:
            raw_text = f"Failed with key {key} at https://x-access-token:***"
            masked = mask_credentials(raw_text)
            self.assertNotIn(key, masked)

    def test_10_real_antigravity_e2e_sync(self):
        # Test 10: Real Antigravity remote environment sync E2E test
        key = get_api_key_by_index(1)
        if not key:
            self.skipTest("No API key available for real E2E test")

        api_key_val = get_api_key_by_index(1)
        client = AntigravityClient(api_key=api_key_val)
        init_res = client.create_interaction(
            prompt="Initialize empty workspace for sync test and reply READY",
            environment="remote"
        )
        self.assertTrue(init_res.get("success"), f"Failed to create test environment: {init_res.get('error')}")

        env_id = init_res.get("environment_id")
        self.assertIsNotNone(env_id)

        # 2. Prepare test local source files including ROLLOVER_TEST.txt
        src_dir = os.path.join(self.test_dir, "e2e_sync_src")
        os.makedirs(src_dir, exist_ok=True)
        
        test_marker_file = os.path.join(src_dir, "ROLLOVER_TEST.txt")
        with open(test_marker_file, "w") as f:
            f.write("KEY1_COMPLETED\n")

        test_code_file = os.path.join(src_dir, "app.py")
        with open(test_code_file, "w") as f:
            f.write("print('Antigravity Workspace Sync Active')\n")

        # 3. Prepare manifest
        manifest, err = self.ws.prepare_source_from_local(src_dir)
        self.assertIsNone(err)

        # 4. Sync to remote environment
        sync_res = self.ws.sync_to_remote(env_id, manifest, overwrite=True)
        self.assertEqual(sync_res.status, SyncStatus.SYNC_SUCCESS, f"Sync failed: {sync_res.error_message}")

        # 5. Verify file content on remote via fresh interaction
        verify_res = client.create_interaction(
            prompt="Please read /workspace/ROLLOVER_TEST.txt and /workspace/app.py and reply with their exact contents.",
            environment="remote",
            environment_id=env_id
        )
        self.assertTrue(verify_res.get("success"))
        output = verify_res.get("output", "")
        self.assertIn("KEY1_COMPLETED", output)
        self.assertIn("Antigravity Workspace Sync Active", output)

if __name__ == "__main__":
    unittest.main()
