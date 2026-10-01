import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor

from keys import KeyPoolManager, AllKeysExhaustedError
from registry import ProjectRegistry
from sessions import SessionStateManager
from client import AntigravityClient
from workspace_sync import WorkspaceSync, SyncStatus, SyncResult, SourceManifest

class TestPhase7_5TenantAwareRouting(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.keys_path = os.path.join(self.test_dir.name, "keys", "key_states.json")
        self.projects_path = os.path.join(self.test_dir.name, "registry", "projects.json")
        self.sessions_path = os.path.join(self.test_dir.name, "sessions", "sessions.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def _make_key_pool(self, cooldown_seconds=60):
        return KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=cooldown_seconds)

    def _make_registry(self):
        return ProjectRegistry(storage_path=self.projects_path, sessions_path=self.sessions_path)

    def _make_session_mgr(self):
        return SessionStateManager(storage_path=self.sessions_path)

    def test_A_same_tenant_selection(self):
        """Test A: key1 & key2 (tenant-A), key3 (tenant-B). When key1 exhausted, select key2 for tenant-A session."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-A",
            "AGY_KEY_3": "secret-3", "AGY_TENANT_3": "tenant-B",
        }
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            pool.report_result("key1", status_code=429) # key1 in COOLDOWN

            key_ref, key_idx = pool.acquire_key(tenant_id="tenant-A")
            self.assertEqual(key_ref, "key2")

    def test_B_never_skip_healthy_same_tenant_key(self):
        """Test B: key1=COOLDOWN (tenant-A), key2=ACTIVE (tenant-A), key3=ACTIVE (tenant-B). key2 MUST be selected before key3."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-A",
            "AGY_KEY_3": "secret-3", "AGY_TENANT_3": "tenant-B",
        }
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            pool.report_result("key1", status_code=429) # key1 COOLDOWN

            key_ref, _ = pool.acquire_key(tenant_id="tenant-A")
            self.assertEqual(key_ref, "key2")

    @patch("workspace_sync.WorkspaceSync.sync_to_remote")
    @patch("client.requests.post")
    def test_C_cross_tenant_only_after_exhaustion(self, mock_post, mock_sync):
        """Test C: tenant-A keys unavailable -> cross-tenant fallback to tenant-B -> new environment required."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B",
        }
        mock_sync.return_value = SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id="env-new-B")

        # key1 429 -> key2 200
        r429 = MagicMock(status_code=429)
        r429.json.return_value = {"error": "rate limit"}
        r200 = MagicMock(status_code=200)
        r200.json.return_value = {"id": "int-B", "environment_id": "env-new-B", "output": "ok"}
        mock_post.side_effect = [r429, r200]

        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            reg = self._make_registry()
            sess_mgr = self._make_session_mgr()

            pdir = os.path.join(self.test_dir.name, "proj")
            os.makedirs(pdir, exist_ok=True)
            reg.register_project(pdir, project_id="proj-1")

            sess_mgr.create_session("proj-1", session_id="sess-1", bound_key="key1", tenant_id="tenant-A", environment_id="env-A")
            sess_mgr.update_session("sess-1", state="ACTIVE")

            client = AntigravityClient(key_pool=pool, project_id="proj-1", registry=reg, session_manager=sess_mgr)
            res = client.create_interaction("prompt", session_id="sess-1")

            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            sess = sess_mgr.get_session("sess-1")
            self.assertEqual(sess["tenant_id"], "tenant-B")
            self.assertEqual(sess["environment_id"], "env-new-B")

            # Verify call 1 sent env-A, call 2 created new environment without env-A
            self.assertEqual(mock_post.call_args_list[0][1]["json"]["environment_id"], "env-A")
            self.assertNotIn("environment_id", mock_post.call_args_list[1][1]["json"])

    @patch("workspace_sync.WorkspaceSync.sync_to_remote")
    @patch("client.requests.post")
    def test_D_tenant_mismatch_fail_fast(self, mock_post, mock_sync):
        """Test D: session tenant=A, selected key tenant=B -> clear old environment & interaction, trigger recovery path."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B",
        }
        mock_sync.return_value = SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id="env-B")

        r200 = MagicMock(status_code=200)
        r200.json.return_value = {"id": "int-B", "environment_id": "env-B", "output": "ok"}
        mock_post.return_value = r200

        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            reg = self._make_registry()
            sess_mgr = self._make_session_mgr()

            # Mark key1 as INACTIVE so pool selects key2 (tenant-B)
            pool.discover_keys()
            pool.report_result("key1", status_code=401)

            sess_mgr.create_session("proj-1", session_id="sess-1", bound_key="key1", tenant_id="tenant-A", environment_id="env-old-A")
            sess_mgr.update_session("sess-1", last_interaction_id="int-old-A", state="ACTIVE")

            client = AntigravityClient(key_pool=pool, project_id="proj-1", registry=reg, session_manager=sess_mgr)
            res = client.create_interaction("prompt", session_id="sess-1")

            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            # Must NOT reuse env-old-A or int-old-A in API payload
            payload = mock_post.call_args[1]["json"]
            self.assertNotIn("environment_id", payload)
            self.assertNotIn("previous_interaction_id", payload)

    @patch("requests.post")
    def test_E_session_binding_atomicity(self, mock_post):
        """Test E: After rollover, bound_key, tenant_id, environment_id, last_interaction_id, state must describe same new context."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-A",
        }
        r429 = MagicMock(status_code=429)
        r429.json.return_value = {"error": "rate limit"}
        r200 = MagicMock(status_code=200)
        r200.json.return_value = {"id": "int-new", "environment_id": "env-1", "output": "ok"}
        mock_post.side_effect = [r429, r200]

        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            reg = self._make_registry()
            sess_mgr = self._make_session_mgr()

            sess_mgr.create_session("proj-1", session_id="sess-1", bound_key="key1", tenant_id="tenant-A", environment_id="env-1")
            sess_mgr.update_session("sess-1", state="ACTIVE")

            client = AntigravityClient(key_pool=pool, project_id="proj-1", registry=reg, session_manager=sess_mgr)
            res = client.create_interaction("prompt", session_id="sess-1")

            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")

            sess = sess_mgr.get_session("sess-1")
            self.assertEqual(sess["bound_key"], "key2")
            self.assertEqual(sess["tenant_id"], "tenant-A")
            self.assertEqual(sess["environment_id"], "env-1")
            self.assertEqual(sess["last_interaction_id"], "int-new")
            self.assertEqual(sess["state"], "ACTIVE")

    @patch("workspace_sync.WorkspaceSync.sync_to_remote")
    @patch("client.requests.post")
    def test_F_workspace_sync_receives_new_tenant_context(self, mock_post, mock_sync):
        """Test F: Instrument WorkspaceSync and assert SessionStateManager fields are updated BEFORE sync is invoked."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B",
        }
        r429 = MagicMock(status_code=429)
        r429.json.return_value = {"error": "rate limit"}
        r200 = MagicMock(status_code=200)
        r200.json.return_value = {"id": "int-fresh", "environment_id": "env-fresh-B", "output": "ok"}
        mock_post.side_effect = [r429, r200]

        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            reg = self._make_registry()
            sess_mgr = self._make_session_mgr()

            pdir = os.path.join(self.test_dir.name, "proj")
            os.makedirs(pdir, exist_ok=True)
            reg.register_project(pdir, project_id="proj-1")

            sess_mgr.create_session("proj-1", session_id="sess-1", bound_key="key1", tenant_id="tenant-A", environment_id="env-A")

            def verify_at_sync_time(env_id, manifest, overwrite=False):
                sess = sess_mgr.get_session("sess-1")
                self.assertEqual(sess["bound_key"], "key2")
                self.assertEqual(sess["tenant_id"], "tenant-B")
                self.assertEqual(sess["environment_id"], "env-fresh-B")
                return SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id=env_id)

            mock_sync.side_effect = verify_at_sync_time

            client = AntigravityClient(key_pool=pool, project_id="proj-1", registry=reg, session_manager=sess_mgr)
            res = client.create_interaction("prompt", session_id="sess-1")
            self.assertTrue(res["success"])
            self.assertEqual(mock_sync.call_count, 1)

    @patch("workspace_sync.WorkspaceSync.sync_to_remote")
    @patch("client.requests.post")
    def test_G_project_registry_remains_metadata_only(self, mock_post, mock_sync):
        """Test G: Assert that projects.json contains NONE of active_key, environment_id, last_interaction_id after operations."""
        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B",
        }
        mock_sync.return_value = SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id="env-B")
        r200 = MagicMock(status_code=200)
        r200.json.return_value = {"id": "int-1", "environment_id": "env-B", "output": "ok"}
        mock_post.return_value = r200

        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            reg = self._make_registry()
            sess_mgr = self._make_session_mgr()

            pdir = os.path.join(self.test_dir.name, "proj")
            os.makedirs(pdir, exist_ok=True)
            reg.register_project(pdir, project_id="proj-1")

            sess_mgr.create_session("proj-1", session_id="sess-1")

            client = AntigravityClient(key_pool=pool, project_id="proj-1", registry=reg, session_manager=sess_mgr)
            client.create_interaction("prompt", session_id="sess-1")

            # Check raw projects.json file on disk
            with open(self.projects_path, "r", encoding="utf-8") as f:
                disk_data = json.load(f)

            proj_entry = disk_data["projects"]["proj-1"]
            self.assertNotIn("active_key", proj_entry)
            self.assertNotIn("environment_id", proj_entry)
            self.assertNotIn("last_interaction_id", proj_entry)

    def test_H_concurrent_tenant_aware_selection(self):
        """Test H: Concurrent multi-threaded tenant-aware key selection to ensure no deadlock, no duplicate invalid binding, no corrupted JSON."""
        env = {
            f"AGY_KEY_{i}": f"secret-{i}" for i in range(1, 11)
        }
        env.update({
            f"AGY_TENANT_{i}": f"tenant-{'A' if i <= 5 else 'B'}" for i in range(1, 11)
        })

        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            sess_mgr = self._make_session_mgr()

            for i in range(1, 11):
                sess_mgr.create_session(f"proj-{i}", session_id=f"sess-{i}", tenant_id=f"tenant-{'A' if i <= 5 else 'B'}")

            def worker_task(task_id):
                tenant = f"tenant-{'A' if task_id % 2 == 0 else 'B'}"
                key_ref, key_idx = pool.acquire_key(tenant_id=tenant)
                # verify tenant matches
                chosen_tenant = pool.get_key_tenant(key_ref)
                self.assertEqual(chosen_tenant, tenant)
                return key_ref

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(worker_task, i) for i in range(20)]
                results = [f.result() for f in futures]

            self.assertEqual(len(results), 20)

            # Check JSON files integrity
            self.assertIsNotNone(pool.get_status())
            self.assertIsNotNone(sess_mgr.load())

    def test_security_audits(self):
        """Test Security Requirements: raw API key not written, tenant_id doesn't expose credentials, cross-tenant env/interaction IDs never reused."""
        secret = "AIzaSySecretApiKeyToNeverWrite"
        env = {
            "AGY_KEY_1": secret, "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B",
        }
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            pool.acquire_key()
            reg = self._make_registry()
            pdir = os.path.join(self.test_dir.name, "proj")
            os.makedirs(pdir, exist_ok=True)
            reg.register_project(pdir, project_id="proj-sec")

            sess_mgr = self._make_session_mgr()
            sess_mgr.create_session("proj-sec", session_id="sess-sec", bound_key="key1", tenant_id="tenant-A")

            # Inspect storage files
            with open(self.keys_path, "r", encoding="utf-8") as f:
                keys_data = f.read()
            with open(self.projects_path, "r", encoding="utf-8") as f:
                proj_data = f.read()
            with open(self.sessions_path, "r", encoding="utf-8") as f:
                sess_data = f.read()

            self.assertNotIn(secret, keys_data)
            self.assertNotIn(secret, proj_data)
            self.assertNotIn(secret, sess_data)

            # Check tenant_id contains no secret substring
            self.assertNotIn(secret, pool.get_key_tenant("key1"))

if __name__ == "__main__":
    unittest.main()
