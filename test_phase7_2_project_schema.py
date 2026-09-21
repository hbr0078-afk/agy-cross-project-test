import os
import json
import tempfile
import unittest
from registry import ProjectRegistry

class TestPhase7_2ProjectSchema(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.projects_path = os.path.join(self.test_dir.name, "projects.json")
        self.sessions_path = os.path.join(self.test_dir.name, "sessions.json")
        self.registry = ProjectRegistry(
            storage_path=self.projects_path,
            sessions_path=self.sessions_path
        )

    def tearDown(self):
        self.test_dir.cleanup()

    def test_01_registered_project_has_no_runtime_fields(self):
        pdir = os.path.join(self.test_dir.name, "my_proj")
        os.makedirs(pdir, exist_ok=True)

        entry = self.registry.register_project(pdir, project_id="proj_clean")
        runtime_keys = {"active_key", "environment_id", "last_interaction_id"}
        for k in runtime_keys:
            self.assertNotIn(k, entry, f"Runtime field '{k}' found in register_project() result")

        with open(self.projects_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        disk_entry = disk_data["projects"]["proj_clean"]
        for k in runtime_keys:
            self.assertNotIn(k, disk_entry, f"Runtime field '{k}' persisted in projects.json")

    def test_02_repeated_registration_purges_injected_runtime_fields(self):
        pdir = os.path.join(self.test_dir.name, "dirty_proj")
        os.makedirs(pdir, exist_ok=True)
        self.registry.register_project(pdir, project_id="proj_dirty")

        # Directly pollute projects.json
        with open(self.projects_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        disk_data["projects"]["proj_dirty"]["active_key"] = "key1"
        disk_data["projects"]["proj_dirty"]["environment_id"] = "env_polluted"
        disk_data["projects"]["proj_dirty"]["last_interaction_id"] = "int_polluted"
        with open(self.projects_path, "w", encoding="utf-8") as f:
            json.dump(disk_data, f)

        # Re-register must purge runtime fields
        cleaned = self.registry.register_project(pdir, project_id="proj_dirty")
        self.assertNotIn("active_key", cleaned)
        self.assertNotIn("environment_id", cleaned)
        self.assertNotIn("last_interaction_id", cleaned)

        with open(self.projects_path, "r", encoding="utf-8") as f:
            disk_data2 = json.load(f)
        disk_entry2 = disk_data2["projects"]["proj_dirty"]
        self.assertNotIn("active_key", disk_entry2)
        self.assertNotIn("environment_id", disk_entry2)
        self.assertNotIn("last_interaction_id", disk_entry2)

if __name__ == "__main__":
    unittest.main()
