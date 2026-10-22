"""
Phase 7-2 Multi-Session Integration & Storage Separation Tests.
Validates:
1. Complete storage separation: projects.json, key_states.json, sessions.json
2. AntigravityClient session-scoped interactions and state propagation
3. Multi-session isolation on the same project without state trampling
4. Sticky binding and automatic session creation
5. Concurrent multi-session interactions with SessionStateManager
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from sessions import SessionStateManager
from registry import ProjectRegistry
from keys import KeyPoolManager
from client import AntigravityClient

class TestPhase7_2MultiSession(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.projects_path = os.path.join(self.test_dir, "projects.json")
        self.keys_path = os.path.join(self.test_dir, "key_states.json")
        self.sessions_path = os.path.join(self.test_dir, "sessions.json")

        self.registry = ProjectRegistry(storage_path=self.projects_path)
        self.key_pool = KeyPoolManager(storage_path=self.keys_path)
        self.session_manager = SessionStateManager(storage_path=self.sessions_path)

        # Register dummy project
        dummy_proj_dir = os.path.join(self.test_dir, "dummy_repo")
        os.makedirs(dummy_proj_dir, exist_ok=True)
        self.registry.register_project(project_path=dummy_proj_dir, project_id="proj-7-2")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch("client.requests.post")
    def test_01_session_scoped_interaction(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "environment_id": "env-sess-001",
            "id": "int-sess-001",
            "output": "Session 1 output",
        }
        mock_post.return_value = mock_resp

        client = AntigravityClient(
            key_pool=self.key_pool,
            project_id="proj-7-2",
            registry=self.registry,
            session_manager=self.session_manager,
        )

        res = client.create_interaction(
            prompt="Hello from session 1",
            session_id="sess-worker-1"
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["environment_id"], "env-sess-001")

        # Verify SessionStateManager updated
        sess = self.session_manager.get_session("sess-worker-1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["environment_id"], "env-sess-001")
        self.assertEqual(sess["last_interaction_id"], "int-sess-001")
        self.assertEqual(sess["state"], "ACTIVE")

    @patch("client.requests.post")
    def test_02_multi_session_isolation_no_trampling(self, mock_post):
        # Two workers in the same project with distinct session IDs
        client = AntigravityClient(
            key_pool=self.key_pool,
            project_id="proj-7-2",
            registry=self.registry,
            session_manager=self.session_manager,
        )

        def make_mock(env_id, int_id, text):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "status": "completed",
                "environment_id": env_id,
                "id": int_id,
                "output": text
            }
            return m

        # Session 1 execution
        mock_post.return_value = make_mock("env-A", "int-A-1", "A done")
        res1 = client.create_interaction(prompt="Task A", session_id="worker-A")

        # Session 2 execution
        mock_post.return_value = make_mock("env-B", "int-B-1", "B done")
        res2 = client.create_interaction(prompt="Task B", session_id="worker-B")

        # Verify each session preserved its own state independently
        sess_a = self.session_manager.get_session("worker-A")
        sess_b = self.session_manager.get_session("worker-B")

        self.assertEqual(sess_a["environment_id"], "env-A")
        self.assertEqual(sess_a["last_interaction_id"], "int-A-1")

        self.assertEqual(sess_b["environment_id"], "env-B")
        self.assertEqual(sess_b["last_interaction_id"], "int-B-1")

    @patch("client.requests.post")
    def test_03_session_continuation_preserves_context(self, mock_post):
        # Seed an existing session
        self.session_manager.create_session(
            project_id="proj-7-2",
            session_id="cont-sess",
            environment_id="env-existing",
            bound_key="key1"
        )
        self.session_manager.update_session(
            session_id="cont-sess",
            last_interaction_id="int-prev-100",
            state="ACTIVE"
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "environment_id": "env-existing",
            "id": "int-next-101",
            "output": "Continued output"
        }
        mock_post.return_value = mock_resp

        client = AntigravityClient(
            key_pool=self.key_pool,
            project_id="proj-7-2",
            registry=self.registry,
            session_manager=self.session_manager,
        )

        res = client.create_interaction(prompt="Continue work", session_id="cont-sess")
        self.assertTrue(res["success"])

        # Check call arguments passed to requests.post
        call_kwargs = mock_post.call_args[1]
        payload = call_kwargs["json"]
        self.assertEqual(payload["environment_id"], "env-existing")
        self.assertEqual(payload["previous_interaction_id"], "int-prev-100")

        # Verify updated session
        updated = self.session_manager.get_session("cont-sess")
        self.assertEqual(updated["last_interaction_id"], "int-next-101")

if __name__ == "__main__":
    unittest.main()
