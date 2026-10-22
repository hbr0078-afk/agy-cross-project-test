import os
import json
import tempfile
import unittest
import shutil
from registry import ProjectRegistry, RegistryCorruptedError
from sessions import SessionStateManager
from client import AntigravityClient
from keys import KeyPoolManager

class TestMigrationAndSourceOfTruth(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.legacy_registry_path = os.path.join(self.test_dir, "registry.json")
        self.projects_path = os.path.join(self.test_dir, "projects.json")
        self.sessions_path = os.path.join(self.test_dir, "sessions.json")
        self.keys_path = os.path.join(self.test_dir, "key_states.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_legacy_registry_migration_flow(self):
        # 1. Create legacy registry.json with v1 data
        legacy_data = {
            "projects": {
                "proj_alpha": {
                    "project_id": "proj_alpha",
                    "path": "/home/ubuntu/alpha",
                    "repo": "https://github.com/org/alpha.git",
                    "branch": "main",
                    "active_key": "key2",
                    "environment_id": "env_alpha_123",
                    "last_interaction_id": "int_alpha_999",
                    "last_commit": "c0ffee1",
                    "state": "IDLE",
                    "roadmap": "ROADMAP.md"
                }
            }
        }
        with open(self.legacy_registry_path, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        # 2. Instantiate ProjectRegistry targeting projects.json
        reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)

        # 3. Verify projects.json created & static metadata preserved
        self.assertTrue(os.path.exists(self.projects_path))
        p = reg.get_project("proj_alpha")
        self.assertIsNotNone(p)
        self.assertEqual(p["path"], "/home/ubuntu/alpha")
        self.assertEqual(p["repo"], "https://github.com/org/alpha.git")
        self.assertEqual(p["branch"], "main")
        self.assertEqual(p["last_commit"], "c0ffee1")

        # 4. Verify sessions.json created & runtime state migrated to default session
        self.assertTrue(os.path.exists(self.sessions_path))
        sess_mgr = SessionStateManager(storage_path=self.sessions_path)
        sess = sess_mgr.get_session("sess_proj_alpha_default")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["project_id"], "proj_alpha")
        self.assertEqual(sess["bound_key"], "key2")
        self.assertEqual(sess["environment_id"], "env_alpha_123")
        self.assertEqual(sess["last_interaction_id"], "int_alpha_999")
        self.assertEqual(sess["state"], "ACTIVE")

        # 5. Verify no raw API key stored anywhere
        with open(self.projects_path, "r") as f:
            proj_content = f.read()
        with open(self.sessions_path, "r") as f:
            sess_content = f.read()
        self.assertNotIn("AIzaSy", proj_content)
        self.assertNotIn("AIzaSy", sess_content)

        # 6. Idempotence: re-instantiate ProjectRegistry, ensure no data corruption or duplicate sessions
        reg2 = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        self.assertEqual(len(reg2.list_projects()), 1)
        self.assertEqual(len(sess_mgr.list_sessions()), 1)

    def test_02_corrupted_legacy_registry_failsafe(self):
        # Corrupted legacy registry
        with open(self.legacy_registry_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json [")

        with self.assertRaises(RegistryCorruptedError):
            ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)

    def test_03_client_session_mode_source_of_truth_separation(self):
        # Create ProjectRegistry & SessionStateManager
        reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        sess_mgr = SessionStateManager(storage_path=self.sessions_path)

        # Register dummy project
        project_dir = os.path.join(self.test_dir, "proj_beta")
        os.makedirs(project_dir, exist_ok=True)
        reg.register_project(project_path=project_dir, project_id="proj_beta")

        # Pre-create two isolated sessions
        sess_a = sess_mgr.create_session("proj_beta", session_id="session-A")
        sess_b = sess_mgr.create_session("proj_beta", session_id="session-B")

        # Mock AntigravityClient with single key
        client = AntigravityClient(
            api_key="mock-key",
            project_id="proj_beta",
            registry=reg,
            session_manager=sess_mgr
        )

        # Mock _execute_request responses
        call_count = [0]
        def mock_exec(api_key, prompt, environment, environment_id, previous_interaction_id, timeout):
            call_count[0] += 1
            if "A" in prompt:
                return {
                    "success": True,
                    "status_code": 200,
                    "environment_id": "env-A",
                    "interaction_id": "INT-A",
                    "output": "Done A"
                }
            else:
                return {
                    "success": True,
                    "status_code": 200,
                    "environment_id": "env-B",
                    "interaction_id": "INT-B",
                    "output": "Done B"
                }

        client._execute_request = mock_exec

        # Execute interaction for session-A
        res_a = client.create_interaction("Prompt A", session_id="session-A")
        self.assertTrue(res_a["success"])

        # Execute interaction for session-B
        res_b = client.create_interaction("Prompt B", session_id="session-B")
        self.assertTrue(res_b["success"])

        # Check sessions.json
        s_a = sess_mgr.get_session("session-A")
        s_b = sess_mgr.get_session("session-B")
        self.assertEqual(s_a["environment_id"], "env-A")
        self.assertEqual(s_a["last_interaction_id"], "INT-A")
        self.assertEqual(s_b["environment_id"], "env-B")
        self.assertEqual(s_b["last_interaction_id"], "INT-B")

        # Check projects.json -> MUST NOT have session-specific runtime last_interaction_id overwritten
        p = reg.get_project("proj_beta")
        self.assertNotEqual(p.get("last_interaction_id"), "INT-A")
        self.assertNotEqual(p.get("last_interaction_id"), "INT-B")

    def test_04_atomic_session_update_consistency(self):
        sess_mgr = SessionStateManager(storage_path=self.sessions_path)
        sess = sess_mgr.create_session("proj_gamma", session_id="sess_atomic")

        # Atomic transaction update
        updated = sess_mgr.update_session(
            session_id="sess_atomic",
            bound_key="key1",
            environment_id="env_atomic_1",
            last_interaction_id="int_atomic_1",
            state="ACTIVE"
        )
        self.assertEqual(updated["environment_id"], "env_atomic_1")
        self.assertEqual(updated["last_interaction_id"], "int_atomic_1")
        self.assertEqual(updated["state"], "ACTIVE")

        # Invalidate
        inv = sess_mgr.invalidate_session("sess_atomic")
        self.assertEqual(inv["state"], "INVALIDATED")

    def test_05_new_project_schema_has_no_runtime_fields(self):
        # Verify that register_project() produces a schema WITHOUT runtime state
        reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        project_dir = os.path.join(self.test_dir, "proj_schema_clean")
        os.makedirs(project_dir, exist_ok=True)
        proj = reg.register_project(project_path=project_dir, project_id="proj_schema_clean")

        # Static fields must be present
        self.assertIn("project_id", proj)
        self.assertIn("path", proj)
        self.assertIn("repo", proj)
        self.assertIn("branch", proj)
        self.assertIn("last_commit", proj)
        self.assertIn("state", proj)
        self.assertIn("roadmap", proj)

        # Runtime session fields MUST NOT be present in projects.json schema
        self.assertNotIn("active_key", proj)
        self.assertNotIn("environment_id", proj)
        self.assertNotIn("last_interaction_id", proj)

        # Also inspect raw JSON stored on disk
        with open(self.projects_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        disk_proj = disk_data["projects"]["proj_schema_clean"]
        self.assertNotIn("active_key", disk_proj)
        self.assertNotIn("environment_id", disk_proj)
        self.assertNotIn("last_interaction_id", disk_proj)

    def test_06_complete_separation_of_three_storages(self):
        # Ensure projects.json, sessions.json, and key_states.json have strict responsibility separation
        reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        sess_mgr = SessionStateManager(storage_path=self.sessions_path)
        key_mgr = KeyPoolManager(storage_path=self.keys_path)

        project_dir = os.path.join(self.test_dir, "proj_tri")
        os.makedirs(project_dir, exist_ok=True)
        reg.register_project(project_path=project_dir, project_id="proj_tri")

        sess = sess_mgr.create_session("proj_tri", session_id="sess_tri_1", bound_key="key1")
        sess_mgr.update_session("sess_tri_1", environment_id="env_tri", last_interaction_id="int_tri", state="ACTIVE")
        key_mgr.report_result("key1", status_code=200)

        # 1. projects.json: Only static project info
        with open(self.projects_path, "r") as f:
            p_data = json.load(f)
        self.assertNotIn("env_tri", json.dumps(p_data))
        self.assertNotIn("int_tri", json.dumps(p_data))

        # 2. sessions.json: Runtime session info, no key health metrics
        with open(self.sessions_path, "r") as f:
            s_data = json.load(f)
        self.assertIn("env_tri", json.dumps(s_data))
        self.assertIn("int_tri", json.dumps(s_data))
        self.assertNotIn("cooldown_until", json.dumps(s_data))

        # 3. key_states.json: Key health only, no environment_id or project_id
        with open(self.keys_path, "r") as f:
            k_data = json.load(f)
        self.assertIn("key1", k_data.get("keys", {}))
        self.assertNotIn("proj_tri", json.dumps(k_data))
        self.assertNotIn("env_tri", json.dumps(k_data))

    def test_07_repeated_register_does_not_reintroduce_runtime_fields(self):
        # Test 2: repeated register does not reintroduce runtime fields
        reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        project_dir = os.path.join(self.test_dir, "proj_repeat")
        os.makedirs(project_dir, exist_ok=True)

        # First registration
        p1 = reg.register_project(project_path=project_dir, project_id="proj_repeat")
        self.assertNotIn("active_key", p1)
        self.assertNotIn("environment_id", p1)
        self.assertNotIn("last_interaction_id", p1)

        # Second registration (re-register)
        p2 = reg.register_project(project_path=project_dir, project_id="proj_repeat")
        self.assertNotIn("active_key", p2)
        self.assertNotIn("environment_id", p2)
        self.assertNotIn("last_interaction_id", p2)

        # Even if someone directly injected legacy fields into disk, re-register strips them
        with open(self.projects_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["projects"]["proj_repeat"]["active_key"] = "key1"
        data["projects"]["proj_repeat"]["environment_id"] = "env_legacy"
        with open(self.projects_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        p3 = reg.register_project(project_path=project_dir, project_id="proj_repeat")
        self.assertNotIn("active_key", p3)
        self.assertNotIn("environment_id", p3)
        self.assertNotIn("last_interaction_id", p3)

if __name__ == "__main__":
    unittest.main()
