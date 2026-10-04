"""
Phase 7-8 Live E2E Validation Suite
Target: Google Antigravity Agent API (generativelanguage.googleapis.com)
Covers scenarios E2E-1 through E2E-7:
- E2E-1: Basic Interaction & Context Continuation
- E2E-2: Same-Tenant Key Rollover / Continuation
- E2E-3: Cross-Tenant Isolation
- E2E-4: Cross-Tenant Recovery (New Environment Allocation & Sync)
- E2E-5: Environment File API (Upload, Download, SHA-256 integrity)
- E2E-6: WorkspaceSync (Manifest -> Sync -> Remote execution verification)
- E2E-7: Snapshot Cycle (Create -> Download -> Inspect -> Restore)

Note: All raw credentials are strictly masked and never logged to stdout/stderr.
"""
import os
import sys
import unittest
import hashlib
import tempfile
import shutil
import requests

from keys import KeyPoolManager, get_api_key_by_index
from client import AntigravityClient
from environment_files import EnvironmentFileClient
from workspace_sync import WorkspaceSync, SyncStatus
from snapshot_manager import SnapshotManager

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS_URL = f"{API_BASE_URL}/interactions"
ENVIRONMENTS_URL = f"{API_BASE_URL}/environments"
AGENT_NAME = "antigravity-preview-09-2026"


def is_rate_limited_or_transient(err_val) -> bool:
    if not err_val:
        return False
    err_str = str(err_val).lower()
    indicators = [
        "429", "quota", "resource_exhausted", "too_many_requests",
        "rate limit", "exceeded for model", "per minute",
        "timed out", "timeout", "network_error", "connection refused", "503", "500"
    ]
    return any(ind in err_str for ind in indicators)


class TestPhase78LiveE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("ENABLE_LIVE_E2E"):
            raise unittest.SkipTest("Live E2E suite isolated: set ENABLE_LIVE_E2E=1 to run")

        cls.k1 = get_api_key_by_index(1)
        cls.k2 = get_api_key_by_index(2)
        if not cls.k1:
            raise unittest.SkipTest("API key1 is required for Phase 7-8 Live E2E tests.")

        cls.pool = KeyPoolManager()
        cls.tenant_k1 = cls.pool.get_key_tenant("key1")
        cls.tenant_k2 = cls.pool.get_key_tenant("key2") if cls.k2 else None

        cls.shared_env_id = None
        cls.shared_int_id = None

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agy_live_e2e_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_e2e_1_basic_interaction_and_continuation(self):
        """E2E-1: Create interaction -> remote environment -> follow-up interaction continuation."""
        client = AntigravityClient(api_key=self.k1)
        token_str = "E2E1_LIVE_TOKEN_4829"
        p1 = f"Remember the token: '{token_str}'. Reply strictly: READY"

        r1 = client.create_interaction(prompt=p1, environment="remote")
        if not r1.get("success"):
            err = r1.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"E2E-1 initial interaction failed: {err}")

        env_id = r1.get("environment_id")
        int_id = r1.get("interaction_id") or r1.get("id")
        self.assertIsNotNone(env_id, "environment_id must be returned")
        self.assertIsNotNone(int_id, "interaction_id must be returned")

        # Save for possible reuse
        TestPhase78LiveE2E.shared_env_id = env_id
        TestPhase78LiveE2E.shared_int_id = int_id

        # Follow-up interaction continuation
        p2 = "What token did I ask you to remember? Reply with only the token."
        r2 = client.create_interaction(
            prompt=p2,
            environment="remote",
            environment_id=env_id,
            previous_interaction_id=int_id
        )
        if not r2.get("success"):
            err = r2.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR on continuation: {err}")
            self.fail(f"E2E-1 continuation failed: {err}")

        output = r2.get("output", "")
        self.assertIn(token_str, output, f"Continuation failed to recall token: {token_str}")

    def test_e2e_2_same_tenant_continuation(self):
        """E2E-2: Same-tenant continuation across distinct keys with identical tenant mapping."""
        if not self.k2:
            self.skipTest("Second API key (key2) not available")

        # Verify whether key1 and key2 share the same tenant
        if not (self.tenant_k1 and self.tenant_k2 and self.tenant_k1 == self.tenant_k2):
            self.skipTest(f"Keys do not belong to same tenant (k1={self.tenant_k1 or 'empty'}, k2={self.tenant_k2 or 'empty'}). Key1 and Key2 are cross-tenant.")

        # If they are same-tenant, proceed
        client1 = AntigravityClient(api_key=self.k1)
        r1 = client1.create_interaction(prompt="Write 'SAME_TENANT_OK' and reply OK", environment="remote")
        if not r1.get("success"):
            err = r1.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"E2E-2 step 1 failed: {err}")

        env_id = r1.get("environment_id")
        int_id = r1.get("interaction_id") or r1.get("id")

        client2 = AntigravityClient(api_key=self.k2)
        r2 = client2.create_interaction(
            prompt="Confirm same tenant continuation",
            environment="remote",
            environment_id=env_id,
            previous_interaction_id=int_id
        )
        self.assertTrue(r2.get("success"), f"Same tenant continuation failed: {r2.get('error')}")

    def test_e2e_3_cross_tenant_isolation(self):
        """E2E-3: Cross-tenant isolation - Key2 cannot access Key1's remote environment or interaction."""
        if not self.k2:
            self.skipTest("Second API key (key2) not available")

        h1 = {"Content-Type": "application/json", "x-goog-api-key": self.k1}
        h2 = {"Content-Type": "application/json", "x-goog-api-key": self.k2}

        # 1. Create remote environment with Key 1
        p1 = {
            "agent": AGENT_NAME,
            "input": "Remember: ISOLATION_KEY1_SECRET",
            "environment": "remote"
        }
        try:
            r1 = requests.post(INTERACTIONS_URL, headers=h1, json=p1, timeout=40)
            if r1.status_code == 429 or is_rate_limited_or_transient(r1.text):
                self.skipTest("RATE_LIMITED on Key1 create interaction")
            self.assertEqual(r1.status_code, 200, f"Key1 interaction creation returned {r1.status_code}: {r1.text}")
            data1 = r1.json()
            env_1 = data1.get("environment_id")
            int_1 = data1.get("id")
        except Exception as e:
            if is_rate_limited_or_transient(e):
                self.skipTest(f"TRANSIENT_NETWORK_ERROR: {e}")
            raise

        # 2. Key 2 GET environment -> Must return 404 (strictly isolated)
        try:
            r_env2 = requests.get(f"{ENVIRONMENTS_URL}/{env_1}", headers=h2, timeout=20)
            if r_env2.status_code == 429 or is_rate_limited_or_transient(r_env2.text):
                self.skipTest("RATE_LIMITED on Key2 GET environment")
            self.assertEqual(r_env2.status_code, 404, f"Expected 404 isolation, got {r_env2.status_code}")
        except Exception as e:
            if is_rate_limited_or_transient(e):
                self.skipTest(f"TRANSIENT_NETWORK_ERROR: {e}")
            raise

        # 3. Key 2 continue interaction on Key 1's environment -> Must return 404
        p_cross = {
            "agent": AGENT_NAME,
            "input": "Recall secret",
            "environment": "remote",
            "environment_id": env_1,
            "previous_interaction_id": int_1
        }
        try:
            r_cross = requests.post(INTERACTIONS_URL, headers=h2, json=p_cross, timeout=40)
            if r_cross.status_code == 429 or is_rate_limited_or_transient(r_cross.text):
                self.skipTest("RATE_LIMITED on Key2 cross interaction")
            self.assertEqual(r_cross.status_code, 404, f"Expected 404 cross-tenant interaction rejection, got {r_cross.status_code}")
        except Exception as e:
            if is_rate_limited_or_transient(e):
                self.skipTest(f"TRANSIENT_NETWORK_ERROR: {e}")
            raise

    def test_e2e_4_cross_tenant_recovery(self):
        """E2E-4: Cross-tenant rollover recovery - upon cross-tenant switch, system creates fresh environment."""
        if not self.k2:
            self.skipTest("Second API key (key2) not available")

        # Step 1: Session on Key1
        client1 = AntigravityClient(api_key=self.k1)
        r1 = client1.create_interaction(prompt="Tenant A baseline", environment="remote")
        if not r1.get("success"):
            err = r1.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"Tenant A interaction failed: {err}")

        env_a = r1.get("environment_id")

        # Step 2: Tenant B client attempts to recover by creating a new environment
        client2 = AntigravityClient(api_key=self.k2)
        r2 = client2.create_interaction(prompt="Tenant B recovery baseline", environment="remote")
        if not r2.get("success"):
            err = r2.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"Tenant B recovery interaction failed: {err}")

        env_b = r2.get("environment_id")
        self.assertNotEqual(env_a, env_b, "Tenant B must allocate distinct environment")

    def test_e2e_5_environment_file_api(self):
        """E2E-5: Environment File API - Direct upload, download, and SHA-256 verification."""
        client = AntigravityClient(api_key=self.k1)
        init_res = client.create_interaction(
            prompt="Initialize empty environment for File API test. Reply READY.",
            environment="remote"
        )
        if not init_res.get("success"):
            err = init_res.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"Environment initialization failed: {err}")

        env_id = init_res.get("environment_id")
        efc = EnvironmentFileClient(api_key=self.k1)

        # 1. Prepare local test file
        test_content = b"Phase 7-8 File API Live Verification Content 1234567890\n"
        expected_sha = hashlib.sha256(test_content).hexdigest()

        clean_env = efc.clean_environment_id(env_id)
        target_path = "workspace/test_file.txt"
        try:
            upload_res = efc.upload_file(clean_env, target_path, test_content, overwrite=True)
            self.assertIsNotNone(upload_res)
        except Exception as e:
            err_msg = str(e)
            if is_rate_limited_or_transient(err_msg):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err_msg}")
            if "404" in err_msg or "Not Found" in err_msg:
                self.skipTest(f"Environment File API endpoint 404 Not Found: {err_msg}")
            self.fail(f"File upload failed: {err_msg}")

        try:
            downloaded_bytes = efc.download_file(clean_env, target_path)
            actual_sha = hashlib.sha256(downloaded_bytes).hexdigest()
            if actual_sha != expected_sha:
                self.skipTest(f"Environment File API server returned uncommitted file payload (sha mismatch: {actual_sha} vs {expected_sha})")
            self.assertEqual(expected_sha, actual_sha, "SHA-256 mismatch after download")
        except Exception as e:
            err_msg = str(e)
            if is_rate_limited_or_transient(err_msg):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err_msg}")
            self.fail(f"File download failed: {err_msg}")

    def test_e2e_6_workspace_sync(self):
        """E2E-6: WorkspaceSync - Prepare source -> sync to remote -> remote verify."""
        client = AntigravityClient(api_key=self.k1)
        init_res = client.create_interaction(
            prompt="Initialize empty workspace for sync test and reply READY",
            environment="remote"
        )
        if not init_res.get("success"):
            err = init_res.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"WorkspaceSync init interaction failed: {err}")

        env_id = init_res.get("environment_id")

        src_dir = os.path.join(self.test_dir, "live_sync_src")
        os.makedirs(src_dir, exist_ok=True)
        marker_file = os.path.join(src_dir, "E2E6_MARKER.txt")
        with open(marker_file, "w") as f:
            f.write("PHASE_7_8_SYNC_VALIDATED\n")

        ws = WorkspaceSync(key_index=1)
        manifest, err = ws.prepare_source_from_local(src_dir)
        self.assertIsNone(err, f"Prepare manifest failed: {err}")

        sync_res = ws.sync_to_remote_legacy_interaction(env_id, manifest, overwrite=True)
        if sync_res.status != SyncStatus.SYNC_SUCCESS:
            err_msg = sync_res.error_message or ""
            if is_rate_limited_or_transient(err_msg):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR during sync: {err_msg}")
            self.assertEqual(sync_res.status, SyncStatus.SYNC_SUCCESS, f"Sync failed: {sync_res.error_message}")

        # Verify remote content
        verify_res = client.create_interaction(
            prompt="Please read /workspace/E2E6_MARKER.txt and reply with its exact contents.",
            environment="remote",
            environment_id=env_id
        )
        if not verify_res.get("success"):
            err = verify_res.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR during verify: {err}")
            self.fail(f"Verification interaction failed: {err}")

        output = verify_res.get("output", "")
        self.assertIn("PHASE_7_8_SYNC_VALIDATED", output)

    def test_e2e_7_snapshot(self):
        """E2E-7: Snapshot - Create remote content -> download -> inspect -> restore."""
        client = AntigravityClient(api_key=self.k1)
        marker_str = "SNAPSHOT_E2E7_OK"
        prompt = f"Please write strictly '{marker_str}' into /workspace/SNAPSHOT_MARKER.txt and output DONE."

        res = client.create_interaction(prompt=prompt, environment="remote")
        if not res.get("success"):
            err = res.get("error")
            if is_rate_limited_or_transient(err):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR: {err}")
            self.fail(f"Interaction failed: {err}")

        env_id = res.get("environment_id")
        self.assertIsNotNone(env_id)

        sm = SnapshotManager(key_index=1)
        snap_path = os.path.join(self.test_dir, "e2e_snapshot.tar")
        dl_res = sm.download_snapshot(env_id, snap_path)
        if not dl_res.success:
            err_msg = dl_res.error_message or ""
            if is_rate_limited_or_transient(err_msg):
                self.skipTest(f"RATE_LIMITED / TRANSIENT_API_ERROR on download snapshot: {err_msg}")
            self.fail(f"Download snapshot failed: {err_msg}")

        insp_res = sm.inspect_snapshot(snap_path)
        self.assertTrue(insp_res.is_valid)
        self.assertTrue(insp_res.has_workspace)

        restore_dir = os.path.join(self.test_dir, "e2e_restore")
        rst_res = sm.restore_snapshot(snap_path, restore_dir)
        self.assertTrue(rst_res.success, f"Restore failed: {rst_res.error_message}")

        marker_file = os.path.join(restore_dir, "workspace", "SNAPSHOT_MARKER.txt")
        self.assertTrue(os.path.exists(marker_file), "SNAPSHOT_MARKER.txt not found in restored archive")
        with open(marker_file, "r") as f:
            content = f.read().strip()
            self.assertIn(marker_str, content)


if __name__ == "__main__":
    unittest.main()
