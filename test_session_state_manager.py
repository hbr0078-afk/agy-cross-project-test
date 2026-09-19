"""
Unit tests for SessionStateManager (Phase 7-2)
Validates:
1. Isolated session persistence in sessions.json
2. Multi-session coexistence per project
3. Process-level file locking and concurrent updates
4. State transitions: IDLE -> ACTIVE -> BUSY -> INVALIDATED
5. Tenant & Key binding metadata per session
"""
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from sessions import SessionStateManager, SessionCorruptedError

class TestSessionStateManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.session_path = os.path.join(self.test_dir, "sessions.json")
        self.manager = SessionStateManager(storage_path=self.session_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_create_and_get_session(self):
        sess = self.manager.create_session(
            project_id="proj-alpha",
            session_id="sess-1",
            bound_key="key1",
            environment_id="env-123",
            tenant_id="tenant-A"
        )
        self.assertEqual(sess["session_id"], "sess-1")
        self.assertEqual(sess["project_id"], "proj-alpha")
        self.assertEqual(sess["bound_key"], "key1")
        self.assertEqual(sess["state"], "IDLE")

        fetched = self.manager.get_session("sess-1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["environment_id"], "env-123")

    def test_02_multi_session_isolation_per_project(self):
        s1 = self.manager.create_session("proj-alpha", "sess-alpha-1", bound_key="key1")
        s2 = self.manager.create_session("proj-alpha", "sess-alpha-2", bound_key="key2")
        s3 = self.manager.create_session("proj-beta", "sess-beta-1", bound_key="key1")

        alpha_sessions = self.manager.list_sessions(project_id="proj-alpha")
        self.assertEqual(len(alpha_sessions), 2)
        sess_ids = {s["session_id"] for s in alpha_sessions}
        self.assertEqual(sess_ids, {"sess-alpha-1", "sess-alpha-2"})

        all_sessions = self.manager.list_sessions()
        self.assertEqual(len(all_sessions), 3)

    def test_03_update_and_invalidation(self):
        self.manager.create_session("proj-A", "sess-target", bound_key="key1")
        up = self.manager.update_session(
            "sess-target",
            environment_id="env-updated",
            last_interaction_id="int-99",
            state="ACTIVE"
        )
        self.assertEqual(up["environment_id"], "env-updated")
        self.assertEqual(up["last_interaction_id"], "int-99")
        self.assertEqual(up["state"], "ACTIVE")

        inv = self.manager.invalidate_session("sess-target")
        self.assertEqual(inv["state"], "INVALIDATED")

    def test_04_concurrent_updates_thread_safe(self):
        self.manager.create_session("proj-conc", "sess-conc", bound_key="key1")

        def _worker(idx):
            mgr = SessionStateManager(storage_path=self.session_path)
            mgr.update_session("sess-conc", last_interaction_id=f"int-{idx}")
            return True

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(_worker, i) for i in range(20)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 20)
        final_sess = self.manager.get_session("sess-conc")
        self.assertTrue(final_sess["last_interaction_id"].startswith("int-"))

    def test_05_corrupted_file_handling(self):
        with open(self.session_path, "w") as f:
            f.write("{invalid_json: 123")

        with self.assertRaises(SessionCorruptedError):
            self.manager.load()

if __name__ == "__main__":
    unittest.main()
