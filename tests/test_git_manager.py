import os
import shutil
import tempfile
import subprocess
import unittest
from git_manager import GitManager, GitState, GitOpStatus, mask_credentials
from registry import ProjectRegistry

class TestGitManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.test_dir, "test_repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        
        # Init git repo with git user config
        subprocess.run(["git", "-C", self.repo_dir, "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.name", "Test Runner"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.email", "test@example.com"], check=True, capture_output=True)
        
        # Create initial commit
        init_file = os.path.join(self.repo_dir, "README.md")
        with open(init_file, "w") as f:
            f.write("# Initial Test Repo\n")
        subprocess.run(["git", "-C", self.repo_dir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "commit", "-m", "Initial commit"], check=True, capture_output=True)
        
        self.gm = GitManager(self.repo_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_clean_state(self):
        # Test 1: No changes -> CLEAN
        res = self.gm.get_status()
        self.assertEqual(res.state, GitState.CLEAN)
        self.assertFalse(res.has_changes)

        # Nothing to commit when clean
        cp = self.gm.commit_checkpoint("no-op checkpoint")
        self.assertEqual(cp.status, GitOpStatus.NOTHING_TO_COMMIT)

    def test_02_changes_detected(self):
        # Test 2: Modify a file -> CHANGES_DETECTED
        test_file = os.path.join(self.repo_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Some new content\n")
            
        res = self.gm.get_status()
        self.assertEqual(res.state, GitState.CHANGES_DETECTED)
        self.assertTrue(res.has_changes)
        self.assertIn("test.txt", res.untracked_files)

    def test_03_checkpoint_commit(self):
        # Test 3: Checkpoint commit succeeds and returns commit SHA
        test_file = os.path.join(self.repo_dir, "checkpoint_test.txt")
        with open(test_file, "w") as f:
            f.write("Checkpoint data\n")

        res = self.gm.commit_checkpoint("checkpoint: key1 test")
        self.assertEqual(res.status, GitOpStatus.COMMITTED)
        self.assertIsNotNone(res.commit_sha)
        self.assertEqual(len(res.commit_sha), 40)
        
        # After checkpoint, working tree should be CLEAN
        st_after = self.gm.get_status()
        self.assertEqual(st_after.state, GitState.CLEAN)

    def test_04_push_and_remote_head(self):
        # Test 4: Setup local bare remote and test push
        bare_remote = os.path.join(self.test_dir, "remote.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", bare_remote], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "remote", "add", "origin", bare_remote], check=True, capture_output=True)

        # Commit a change
        fpath = os.path.join(self.repo_dir, "file_to_push.txt")
        with open(fpath, "w") as f:
            f.write("Pushed content\n")
        cp = self.gm.commit_checkpoint("checkpoint: push test")
        self.assertEqual(cp.status, GitOpStatus.COMMITTED)

        # Push to remote
        p_res = self.gm.push(remote="origin", branch="main")
        self.assertEqual(p_res.status, GitOpStatus.PUSHED)

        # Verify remote HEAD matches local commit SHA
        rem_head = subprocess.run(["git", "--git-dir", bare_remote, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(rem_head, cp.commit_sha)

    def test_05_invalid_repository(self):
        # Test 5: Invalid repository handling
        invalid_gm = GitManager(os.path.join(self.test_dir, "non_existent_folder"))
        st = invalid_gm.get_status()
        self.assertEqual(st.state, GitState.INVALID_REPOSITORY)
        
        cp = invalid_gm.commit_checkpoint("msg")
        self.assertEqual(cp.status, GitOpStatus.INVALID_REPOSITORY)

    def test_06_invalid_branch(self):
        # Test 6: Invalid branch handling
        res = self.gm.push(branch="non_existent_branch_xyz")
        self.assertEqual(res.status, GitOpStatus.PUSH_FAILED)

    def test_07_push_failure_detection(self):
        # Test 7: Push failure when remote doesn't exist or is unreachable
        subprocess.run(["git", "-C", self.repo_dir, "remote", "add", "broken_remote", "https://invalid.domain.local/broken.git"], check=True, capture_output=True)
        res = self.gm.push(remote="broken_remote", branch="main")
        self.assertEqual(res.status, GitOpStatus.PUSH_FAILED)

    def test_08_merge_conflict_safety(self):
        # Test 8: Merge conflict during pull is captured without crashing
        bare_remote = os.path.join(self.test_dir, "conflict_remote.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", bare_remote], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "remote", "add", "origin", bare_remote], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "push", "-u", "origin", "main"], check=True, capture_output=True)

        # Clone repo 2
        repo2_dir = os.path.join(self.test_dir, "repo2")
        subprocess.run(["git", "clone", bare_remote, repo2_dir], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo2_dir, "config", "user.name", "Test Runner 2"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo2_dir, "config", "user.email", "test2@example.com"], check=True, capture_output=True)

        # In repo 2, modify README.md and push
        with open(os.path.join(repo2_dir, "README.md"), "w") as f:
            f.write("# Modified in Repo 2\n")
        subprocess.run(["git", "-C", repo2_dir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo2_dir, "commit", "-m", "Repo2 update"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo2_dir, "push", "origin", "main"], check=True, capture_output=True)

        # In repo 1, modify README.md conflictingly and commit
        with open(os.path.join(self.repo_dir, "README.md"), "w") as f:
            f.write("# Conflicting in Repo 1\n")
        subprocess.run(["git", "-C", self.repo_dir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo_dir, "commit", "-m", "Repo1 update"], check=True, capture_output=True)

        # Try to pull in repo 1 -> should report CONFLICT cleanly without destroying files
        pull_res = self.gm.pull(remote="origin", branch="main")
        self.assertEqual(pull_res.status, GitOpStatus.CONFLICT)

    def test_09_secret_masking(self):
        # Test 9: Verify tokens/keys are masked in outputs and logs
        raw_token_text = "fatal: repository 'https://x-access-token:ghp_1234567890abcdefghijklmnopqrstuvwxyz@github.com/org/repo.git' not found"
        masked = mask_credentials(raw_token_text)
        self.assertNotIn("ghp_1234567890abcdefghijklmnopqrstuvwxyz", masked)
        self.assertIn("https://x-access-token:***@github.com", masked)

        raw_aiza = "error with key AIzaSyD9876543210ZYXWVUTSRQPONMLKJIHGFEDCBA"
        masked_aiza = mask_credentials(raw_aiza)
        self.assertNotIn("AIzaSyD9876543210ZYXWVUTSRQPONMLKJIHGFEDCBA", masked_aiza)

    def test_10_registry_state_resilience(self):
        # Test 10: Registry state stays intact during git operations
        reg_file = os.path.join(self.test_dir, "reg.json")
        reg = ProjectRegistry(storage_path=reg_file)
        reg.register_project(self.repo_dir, project_id="git-test-proj")
        
        # Perform checkpoint
        fpath = os.path.join(self.repo_dir, "reg_test.txt")
        with open(fpath, "w") as f:
            f.write("state resilience\n")
        cp = self.gm.commit_checkpoint("checkpoint: reg test")
        self.assertEqual(cp.status, GitOpStatus.COMMITTED)

        # Update registry with commit SHA
        reg.update_project_state("git-test-proj", last_commit=cp.commit_sha)

        # Re-read registry
        restored = reg.get_project("git-test-proj")
        self.assertEqual(restored["last_commit"], cp.commit_sha)
        self.assertEqual(restored["state"], "IDLE")

if __name__ == "__main__":
    unittest.main()
