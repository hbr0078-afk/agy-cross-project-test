import os
import json
import tempfile
import unittest
from registry import ProjectRegistry, RegistryCorruptedError

class TestProjectRegistry(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.reg_file = os.path.join(self.test_dir, "test_registry.json")
        self.proj_a = os.path.join(self.test_dir, "project_a")
        self.proj_b = os.path.join(self.test_dir, "project_b")
        os.makedirs(self.proj_a, exist_ok=True)
        os.makedirs(self.proj_b, exist_ok=True)
        self.registry = ProjectRegistry(storage_path=self.reg_file)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_register_and_status(self):
        # 1. Register project
        entry = self.registry.register_project(self.proj_a, project_id="proj-a")
        self.assertEqual(entry["project_id"], "proj-a")
        self.assertEqual(entry["state"], "IDLE")

        # 2. Get status
        fetched = self.registry.get_project("proj-a")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["path"], self.proj_a)

    def test_update_environment_and_interaction(self):
        self.registry.register_project(self.proj_a, project_id="proj-a")
        
        # 3. Update static fields only (last_commit, state)
        # Runtime fields (environment_id, last_interaction_id) are NOT stored in projects.json (Phase 7-2 contract)
        updated = self.registry.update_project_state(
            "proj-a",
            last_commit="commit-abcdef",
            state="RUNNING"
        )
        # Runtime fields should NOT be in projects.json
        self.assertNotIn("environment_id", updated)
        self.assertNotIn("last_interaction_id", updated)
        self.assertEqual(updated["last_commit"], "commit-abcdef")
        self.assertEqual(updated["state"], "RUNNING")

        # 5 & 6. Persistence across new instance
        new_registry = ProjectRegistry(storage_path=self.reg_file)
        restored = new_registry.get_project("proj-a")
        self.assertNotIn("environment_id", restored)
        self.assertNotIn("last_interaction_id", restored)
        self.assertEqual(restored["last_commit"], "commit-abcdef")
        self.assertEqual(restored["state"], "RUNNING")
        self.assertEqual(restored["last_commit"], "commit-abcdef")

    def test_two_projects_isolation(self):
        # 7. Check 2 projects are isolated
        self.registry.register_project(self.proj_a, project_id="proj-a")
        self.registry.register_project(self.proj_b, project_id="proj-b")

        self.registry.update_project_state("proj-a", state="RUNNING")
        self.registry.update_project_state("proj-b", state="PAUSED")

        proj_a = self.registry.get_project("proj-a")
        proj_b = self.registry.get_project("proj-b")

        # Runtime fields (environment_id) should NOT be in projects.json (Phase 7-2 contract)
        self.assertNotIn("environment_id", proj_a)
        self.assertNotIn("environment_id", proj_b)
        self.assertEqual(proj_a["state"], "RUNNING")
        self.assertEqual(proj_b["state"], "PAUSED")

    def test_corrupted_file_handling(self):
        # 8. Corrupted state file handling
        with open(self.reg_file, "w", encoding="utf-8") as f:
            f.write("{invalid_json: true, broken")

        with self.assertRaises(RegistryCorruptedError):
            self.registry.load()

    def test_no_secret_keys_stored(self):
        # 9. Verify no secrets in state file
        self.registry.register_project(self.proj_a, project_id="proj-a")
        with open(self.reg_file, "r", encoding="utf-8") as f:
            raw_text = f.read()
        self.assertNotIn("AIza", raw_text)
        self.assertNotIn("key_value", raw_text)
        # Note: In Phase 7-2, runtime fields including active_key are no longer stored in projects.json
        self.assertNotIn("active_key", raw_text)

if __name__ == "__main__":
    unittest.main()
