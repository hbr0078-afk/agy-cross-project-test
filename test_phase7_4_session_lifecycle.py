"""
Phase 7-4 Session Lifecycle & Fallback Recovery Tests.
Validates:
1. Session state transitions (IDLE -> ACTIVE -> INVALIDATED)
2. Same-Tenant Key Rollover (preserving environment/context)
3. Cross-Tenant Fallback (invalidation, new environment, WorkspaceSync)
4. Environment/Interaction Recovery logic
5. Atomic session updates
"""
import os
import unittest
import tempfile
import shutil
from unittest.mock import patch, MagicMock

from sessions import SessionStateManager
from keys import KeyPoolManager
from registry import ProjectRegistry
from client import AntigravityClient
from workspace_sync import WorkspaceSync, SyncStatus, SyncResult

class TestPhase7_4SessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.sessions_path = os.path.join(self.test_dir, "sessions.json")
        self.keys_path = os.path.join(self.test_dir, "key_states.json")
        self.registry_path = os.path.join(self.test_dir, "projects.json")

        self.session_manager = SessionStateManager(storage_path=self.sessions_path)
        self.key_pool = KeyPoolManager(storage_path=self.keys_path)
        self.registry = ProjectRegistry(storage_path=self.registry_path)

        # Register a dummy project
        self.proj_dir = os.path.join(self.test_dir, "my_project")
        os.makedirs(self.proj_dir, exist_ok=True)
        self.registry.register_project(self.proj_dir, project_id="proj-1")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_create_and_get_session_idle(self):
        # Test 1: New session starts in IDLE
        sess = self.session_manager.create_session(project_id="proj-1", session_id="sess-1")
        self.assertEqual(sess["state"], "IDLE")
        self.assertEqual(sess["bound_key"], "key1")

    @patch("client.requests.post")
    def test_02_idle_to_active_transition(self, mock_post):
        # Test 2: IDLE -> ACTIVE after successful interaction
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "environment_id": "env-1",
            "id": "int-1",
            "output": "hello"
        }
        mock_post.return_value = mock_resp

        env = {"AGY_KEY_1": "secret-1"}
        with patch.dict(os.environ, env, clear=True):
            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id="proj-1",
                registry=self.registry,
                session_manager=self.session_manager
            )
            res = client.create_interaction(prompt="hi", session_id="sess-1")
            self.assertTrue(res["success"])
            
            sess = self.session_manager.get_session("sess-1")
            self.assertEqual(sess["state"], "ACTIVE")
            self.assertEqual(sess["environment_id"], "env-1")
            self.assertEqual(sess["last_interaction_id"], "int-1")

    def test_03_active_to_invalidated_transition(self):
        # Test 3: ACTIVE -> INVALIDATED explicitly
        self.session_manager.create_session(project_id="proj-1", session_id="sess-1")
        self.session_manager.update_session("sess-1", state="ACTIVE")
        
        inv = self.session_manager.invalidate_session("sess-1")
        self.assertEqual(inv["state"], "INVALIDATED")

    @patch("client.requests.post")
    def test_04_same_tenant_429_rollover(self, mock_post):
        # Test 4: Key1 (Tenant A) 429 -> Key2 (Tenant A) Success -> Same environment
        resp_429 = MagicMock(status_code=429)
        resp_429.json.return_value = {"error": {"message": "Rate limit"}}
        
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "status": "completed",
            "environment_id": "env-shared",
            "id": "int-2",
            "output": "recovered"
        }
        mock_post.side_effect = [resp_429, resp_200]

        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-A"
        }
        with patch.dict(os.environ, env, clear=True):
            # Pre-bind session to key1 and env-shared
            self.session_manager.create_session(
                project_id="proj-1", 
                session_id="sess-1", 
                bound_key="key1",
                tenant_id="tenant-A",
                environment_id="env-shared"
            )
            self.session_manager.update_session("sess-1", state="ACTIVE")

            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id="proj-1",
                registry=self.registry,
                session_manager=self.session_manager
            )
            
            res = client.create_interaction(prompt="retry", session_id="sess-1")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")
            
            sess = self.session_manager.get_session("sess-1")
            self.assertEqual(sess["bound_key"], "key2")
            self.assertEqual(sess["environment_id"], "env-shared")
            self.assertEqual(sess["state"], "ACTIVE")
            
            # Verify both calls used same environment_id
            self.assertEqual(mock_post.call_count, 2)
            for call in mock_post.call_args_list:
                self.assertEqual(call[1]["json"]["environment_id"], "env-shared")

    @patch("client.requests.post")
    @patch("workspace_sync.WorkspaceSync.sync_to_remote")
    def test_05_cross_tenant_fallback(self, mock_sync, mock_post):
        # Test 5: Key1 (Tenant A) unavailable -> Key2 (Tenant B) -> Invalidate, New Env, Sync
        
        # Key1 fails (e.g. 429)
        resp_429 = MagicMock(status_code=429)
        resp_429.json.return_value = {"error": {"message": "Rate limit"}}
        
        # Key2 succeeds
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "status": "completed",
            "environment_id": "env-new-tenant-B",
            "id": "int-new-1",
            "output": "fresh start"
        }
        mock_post.side_effect = [resp_429, resp_200]
        
        # WorkspaceSync mock
        mock_sync.return_value = SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id="env-new-tenant-B")

        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B"
        }
        with patch.dict(os.environ, env, clear=True):
            # Pre-bind session to key1 and env-A
            self.session_manager.create_session(
                project_id="proj-1", 
                session_id="sess-1", 
                bound_key="key1",
                tenant_id="tenant-A",
                environment_id="env-A"
            )
            self.session_manager.update_session("sess-1", state="ACTIVE")

            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id="proj-1",
                registry=self.registry,
                session_manager=self.session_manager
            )
            
            res = client.create_interaction(prompt="fallback", session_id="sess-1")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")
            
            sess = self.session_manager.get_session("sess-1")
            self.assertEqual(sess["bound_key"], "key2")
            self.assertEqual(sess["tenant_id"], "tenant-B")
            self.assertEqual(sess["environment_id"], "env-new-tenant-B")
            self.assertEqual(sess["state"], "ACTIVE")
            
            # Verify first call used env-A, second call created a fresh environment (no environment_id sent)
            self.assertEqual(mock_post.call_count, 2)
            self.assertEqual(mock_post.call_args_list[0][1]["json"]["environment_id"], "env-A")
            self.assertNotIn("environment_id", mock_post.call_args_list[1][1]["json"])
            
            # Verify WorkspaceSync was called for the new environment
            mock_sync.assert_called_once()
            self.assertEqual(mock_sync.call_args[0][0], "env-new-tenant-B")

    @patch("client.requests.post")
    def test_06_cross_tenant_404_prevention(self, mock_post):
        # Test 6: If current session tenant != chosen key tenant, skip continuation and start fresh
        
        # Success on new tenant
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "status": "completed",
            "environment_id": "env-B",
            "id": "int-B-1",
            "output": "prevented 404"
        }
        mock_post.return_value = mock_resp

        env = {
            "AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A",
            "AGY_KEY_2": "secret-2", "AGY_TENANT_2": "tenant-B"
        }
        with patch.dict(os.environ, env, clear=True):
            # Session exists but key1 is exhausted/invalid, so key2 will be picked
            # We simulate key1 being INACTIVE
            kp = self.key_pool
            kp.discover_keys()
            kp.report_result("key1", status_code=401) 

            self.session_manager.create_session(
                project_id="proj-1", 
                session_id="sess-1", 
                bound_key="key1",
                tenant_id="tenant-A",
                environment_id="env-A"
            )
            self.session_manager.update_session("sess-1", state="ACTIVE")

            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id="proj-1",
                registry=self.registry,
                session_manager=self.session_manager
            )
            
            # Mock WorkspaceSync to avoid real sync
            with patch("workspace_sync.WorkspaceSync.sync_to_remote") as mock_sync:
                mock_sync.return_value = SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id="env-B")
                
                res = client.create_interaction(prompt="fresh", session_id="sess-1")
                self.assertTrue(res["success"])
                self.assertEqual(res["key_ref"], "key2")
                
                # Should NOT have tried to use env-A because of tenant mismatch; should start fresh without environment_id
                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                self.assertNotIn("environment_id", payload)
                self.assertNotIn("previous_interaction_id", payload)

    @patch("client.requests.post")
    def test_07_same_tenant_interaction_recovery(self, mock_post):
        # Test 7: Interaction 404 (Interaction missing) -> Keep environment, Reset Interaction
        
        # Interaction fails with 404
        resp_404 = MagicMock(status_code=404)
        resp_404.json.return_value = {"error": {"message": "Requested entity was not found."}}
        
        # Second attempt without interaction id succeeds
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "status": "completed",
            "environment_id": "env-1",
            "id": "int-new",
            "output": "recovered interaction"
        }
        mock_post.side_effect = [resp_404, resp_200]

        env = {"AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A"}
        with patch.dict(os.environ, env, clear=True):
            self.session_manager.create_session(
                project_id="proj-1", 
                session_id="sess-1", 
                bound_key="key1",
                tenant_id="tenant-A",
                environment_id="env-1"
            )
            self.session_manager.update_session("sess-1", last_interaction_id="int-old", state="ACTIVE")

            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id="proj-1",
                registry=self.registry,
                session_manager=self.session_manager
            )
            
            res = client.create_interaction(prompt="fix interaction", session_id="sess-1")
            self.assertTrue(res["success"])
            self.assertEqual(res["interaction_id"], "int-new")
            
            # Verify environment was preserved
            self.assertEqual(mock_post.call_count, 2)
            self.assertEqual(mock_post.call_args_list[0][1]["json"]["previous_interaction_id"], "int-old")
            self.assertNotIn("previous_interaction_id", mock_post.call_args_list[1][1]["json"])
            self.assertEqual(mock_post.call_args_list[1][1]["json"]["environment_id"], "env-1")

    @patch("client.requests.post")
    @patch("workspace_sync.WorkspaceSync.sync_to_remote")
    def test_08_environment_loss_recovery(self, mock_sync, mock_post):
        # Test 8: Environment 404 (Environment missing) -> Invalidate, New Env, Sync, New Interaction
        
        # Interaction/Env fails with 404
        resp_404 = MagicMock(status_code=404)
        resp_404.json.return_value = {"error": {"message": "Requested entity was not found."}}
        
        # Fresh success
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "status": "completed",
            "environment_id": "env-fresh",
            "id": "int-fresh",
            "output": "env recovered"
        }
        mock_post.side_effect = [resp_404, resp_200]
        
        mock_sync.return_value = SyncResult(status=SyncStatus.SYNC_SUCCESS, environment_id="env-fresh")

        env = {"AGY_KEY_1": "secret-1", "AGY_TENANT_1": "tenant-A"}
        with patch.dict(os.environ, env, clear=True):
            self.session_manager.create_session(
                project_id="proj-1", 
                session_id="sess-1", 
                bound_key="key1",
                tenant_id="tenant-A",
                environment_id="env-old"
            )
            self.session_manager.update_session("sess-1", state="ACTIVE")

            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id="proj-1",
                registry=self.registry,
                session_manager=self.session_manager
            )
            
            res = client.create_interaction(prompt="fix env", session_id="sess-1")
            self.assertTrue(res["success"])
            
            sess = self.session_manager.get_session("sess-1")
            self.assertEqual(sess["environment_id"], "env-fresh")
            self.assertEqual(sess["state"], "ACTIVE")
            
            # Verify WorkspaceSync was called for the new environment
            mock_sync.assert_called_once()
            self.assertEqual(mock_sync.call_args[0][0], "env-fresh")

if __name__ == "__main__":
    unittest.main()
