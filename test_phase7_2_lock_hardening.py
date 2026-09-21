import os
import json
import time
import tempfile
import unittest
import threading
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

from file_lock import FileAndThreadLock, get_shared_thread_lock
from registry import ProjectRegistry
from keys import KeyPoolManager
from sessions import SessionStateManager
from client import AntigravityClient

class TestPhase7_2LockHardening(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.projects_path = os.path.join(self.test_dir.name, "projects.json")
        self.keys_path = os.path.join(self.test_dir.name, "key_states.json")
        self.sessions_path = os.path.join(self.test_dir.name, "sessions.json")

    def tearDown(self):
        self.test_dir.cleanup()

    # -------------------------------------------------------------------------
    # Test 1: Same ProjectRegistry instance across multi-threads
    # -------------------------------------------------------------------------
    def test_01_same_registry_instance_multithread(self):
        registry = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        errors = []

        def worker(idx):
            try:
                pdir = os.path.join(self.test_dir.name, f"proj_{idx}")
                os.makedirs(pdir, exist_ok=True)
                registry.register_project(pdir, project_id=f"proj_{idx}")
                registry.update_project_state(f"proj_{idx}", state="ACTIVE")
                p = registry.get_project(f"proj_{idx}")
                if not p or p.get("state") != "ACTIVE":
                    errors.append(f"Mismatch in proj_{idx}")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(worker, i) for i in range(30)]
            for f in futures:
                f.result(timeout=10)

        self.assertEqual(errors, [])
        all_projs = registry.list_projects()
        self.assertEqual(len(all_projs), 30)

    # -------------------------------------------------------------------------
    # Test 2: Distinct ProjectRegistry instances sharing same projects.json
    # -------------------------------------------------------------------------
    def test_02_multi_instance_registry_multithread(self):
        errors = []

        def worker(idx):
            try:
                reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
                pdir = os.path.join(self.test_dir.name, f"multi_inst_{idx}")
                os.makedirs(pdir, exist_ok=True)
                reg.register_project(pdir, project_id=f"multi_{idx}")
                reg.update_project_state(f"multi_{idx}", state="TEST")
                p = reg.get_project(f"multi_{idx}")
                if not p or p.get("state") != "TEST":
                    errors.append(f"Mismatch in multi_{idx}")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=25) as ex:
            futures = [ex.submit(worker, i) for i in range(40)]
            for f in futures:
                f.result(timeout=10)

        self.assertEqual(errors, [])
        final_reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
        self.assertEqual(len(final_reg.list_projects()), 40)

    # -------------------------------------------------------------------------
    # Test 3: Same KeyPoolManager instance across multi-threads
    # -------------------------------------------------------------------------
    def test_03_same_key_pool_instance_multithread(self):
        env = {f"AGY_KEY_{i}": f"secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=60)
            errors = []

            def worker(idx):
                try:
                    key_ref, key_idx = pool.acquire_key()
                    pool.report_result(key_ref, status_code=200)
                except Exception as e:
                    errors.append(str(e))

            with ThreadPoolExecutor(max_workers=20) as ex:
                futures = [ex.submit(worker, i) for i in range(50)]
                for f in futures:
                    f.result(timeout=10)

            self.assertEqual(errors, [])
            status = pool.get_status()
            self.assertEqual(len(status), 10)

    # -------------------------------------------------------------------------
    # Test 4: Distinct KeyPoolManager instances sharing same key_states.json
    # -------------------------------------------------------------------------
    def test_04_multi_instance_key_pool_multithread(self):
        env = {f"AGY_KEY_{i}": f"secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            errors = []

            def worker(idx):
                try:
                    pool = KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=60)
                    key_ref, key_idx = pool.acquire_key()
                    pool.report_result(key_ref, status_code=200)
                except Exception as e:
                    errors.append(str(e))

            with ThreadPoolExecutor(max_workers=25) as ex:
                futures = [ex.submit(worker, i) for i in range(50)]
                for f in futures:
                    f.result(timeout=10)

            self.assertEqual(errors, [])
            final_pool = KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=60)
            self.assertEqual(len(final_pool.get_status()), 10)

    # -------------------------------------------------------------------------
    # Test 5: Distinct SessionStateManager instances sharing same sessions.json
    # -------------------------------------------------------------------------
    def test_05_multi_instance_session_state_manager_multithread(self):
        errors = []

        def worker(idx):
            try:
                mgr = SessionStateManager(storage_path=self.sessions_path)
                sid = f"sess_{idx}"
                mgr.create_session(project_id="proj_shared", session_id=sid, bound_key="key1")
                mgr.update_session(session_id=sid, environment_id=f"env_{idx}", last_interaction_id=f"int_{idx}", state="ACTIVE")
                s = mgr.get_session(sid)
                if not s or s.get("state") != "ACTIVE" or s.get("environment_id") != f"env_{idx}":
                    errors.append(f"Mismatch in sess_{idx}")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=25) as ex:
            futures = [ex.submit(worker, i) for i in range(50)]
            for f in futures:
                f.result(timeout=10)

        self.assertEqual(errors, [])
        final_mgr = SessionStateManager(storage_path=self.sessions_path)
        self.assertEqual(len(final_mgr.list_sessions()), 50)

    # -------------------------------------------------------------------------
    # Test 6: High Concurrency (50 threads) Stress Read/Write
    # -------------------------------------------------------------------------
    def test_06_high_concurrency_stress_read_write(self):
        errors = []

        def worker(idx):
            try:
                # Interleaved read/write operations
                reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
                sess = SessionStateManager(storage_path=self.sessions_path)

                pdir = os.path.join(self.test_dir.name, f"stress_{idx % 5}")
                os.makedirs(pdir, exist_ok=True)
                reg.register_project(pdir, project_id=f"stress_{idx % 5}")

                sid = f"stress_sess_{idx}"
                sess.create_session(project_id=f"stress_{idx % 5}", session_id=sid)
                sess.update_session(sid, last_interaction_id=f"int_{idx}")

                # Read verification
                _ = reg.list_projects()
                _ = sess.list_sessions()
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=30) as ex:
            futures = [ex.submit(worker, i) for i in range(60)]
            for f in futures:
                f.result(timeout=15)

        self.assertEqual(errors, [])

    # -------------------------------------------------------------------------
    # Test 7: Inter-Storage Non-Interference (projects vs keys vs sessions)
    # -------------------------------------------------------------------------
    def test_07_concurrent_multi_storage_deadlock_free(self):
        env = {f"AGY_KEY_{i}": f"secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            errors = []

            def worker_reg(idx):
                try:
                    reg = ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)
                    pdir = os.path.join(self.test_dir.name, f"deadlock_p_{idx}")
                    os.makedirs(pdir, exist_ok=True)
                    reg.register_project(pdir, project_id=f"deadlock_p_{idx}")
                except Exception as e:
                    errors.append(f"reg error: {e}")

            def worker_key(idx):
                try:
                    pool = KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=60)
                    k_ref, _ = pool.acquire_key()
                    pool.report_result(k_ref, status_code=200)
                except Exception as e:
                    errors.append(f"key error: {e}")

            def worker_sess(idx):
                try:
                    mgr = SessionStateManager(storage_path=self.sessions_path)
                    mgr.create_session(project_id="deadlock_p_0", session_id=f"deadlock_s_{idx}")
                    mgr.update_session(f"deadlock_s_{idx}", state="ACTIVE")
                except Exception as e:
                    errors.append(f"sess error: {e}")

            with ThreadPoolExecutor(max_workers=30) as ex:
                futures = []
                for i in range(20):
                    futures.append(ex.submit(worker_reg, i))
                    futures.append(ex.submit(worker_key, i))
                    futures.append(ex.submit(worker_sess, i))
                for f in futures:
                    f.result(timeout=15)

            self.assertEqual(errors, [])

    # -------------------------------------------------------------------------
    # Test 8: Cross-Process Multiprocessing File Lock Validation
    # -------------------------------------------------------------------------
    def test_08_cross_process_locking_and_json_integrity(self):
        code = """
import sys, os
from sessions import SessionStateManager

storage_path = sys.argv[1]
proc_id = sys.argv[2]

mgr = SessionStateManager(storage_path=storage_path)
for i in range(10):
    sid = f"proc_{proc_id}_sess_{i}"
    mgr.create_session(project_id=f"proc_{proc_id}", session_id=sid, bound_key="key1")
    mgr.update_session(session_id=sid, environment_id=f"env_{proc_id}_{i}", state="ACTIVE")
"""
        procs = []
        for p in range(5):
            proc = subprocess.Popen(
                [sys.executable, "-c", code, self.sessions_path, str(p)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            procs.append(proc)

        for p in procs:
            stdout, stderr = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, f"Subprocess failed with stderr: {stderr}")

        # Check total created sessions = 5 processes * 10 sessions = 50
        mgr = SessionStateManager(storage_path=self.sessions_path)
        sessions = mgr.list_sessions()
        self.assertEqual(len(sessions), 50)

    # -------------------------------------------------------------------------
    # Test 9: Same-thread nested acquisition across DIFFERENT FileAndThreadLock instances
    # -------------------------------------------------------------------------
    def test_09_same_thread_nested_different_wrapper_instances(self):
        lock_path = os.path.join(self.test_dir.name, "nested.lock")
        lock1 = FileAndThreadLock(lock_path)
        lock2 = FileAndThreadLock(lock_path)
        lock3 = FileAndThreadLock(lock_path)

        executed = []
        with lock1:
            executed.append("outer")
            with lock2:
                executed.append("middle")
                with lock3:
                    executed.append("inner")

        self.assertEqual(executed, ["outer", "middle", "inner"])

if __name__ == "__main__":
    unittest.main()
