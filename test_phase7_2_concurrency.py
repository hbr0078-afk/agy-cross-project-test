"""
Concurrency validation for Phase 7-2 SessionStateManager & Multi-Session Isolation.
Simulates multiple concurrent threads accessing and mutating sessions across projects
and verifies zero race conditions, deadlocks, data corruption, or cross-talk.
"""
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from sessions import SessionStateManager
from client import AntigravityClient
from unittest.mock import MagicMock

class TestPhase7_2Concurrency(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.sessions_path = os.path.join(self.test_dir, "sessions.json")
        self.manager = SessionStateManager(storage_path=self.sessions_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_concurrent_multi_session_creation_and_updates(self):
        # 10 workers simultaneously creating and updating distinct sessions
        def _worker(worker_id):
            mgr = SessionStateManager(storage_path=self.sessions_path)
            sid = f"worker_sess_{worker_id}"
            pid = f"proj_{worker_id % 3}"
            mgr.create_session(project_id=pid, session_id=sid, bound_key="key1")
            
            for step in range(5):
                mgr.update_session(
                    session_id=sid,
                    environment_id=f"env_{worker_id}",
                    last_interaction_id=f"int_{worker_id}_{step}",
                    state="BUSY" if step < 4 else "ACTIVE"
                )
            return True

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_worker, i) for i in range(20)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 20)
        self.assertTrue(all(results))

        # Check total sessions
        all_sess = self.manager.list_sessions()
        self.assertEqual(len(all_sess), 20)
        for s in all_sess:
            self.assertEqual(s["state"], "ACTIVE")
            self.assertTrue(s["last_interaction_id"].endswith("_4"))

if __name__ == "__main__":
    unittest.main()
