import os
import json
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor

from keys import KeyPoolManager, AllKeysExhaustedError
from registry import ProjectRegistry
from client import AntigravityClient
from workspace_sync import WorkspaceSync, SyncStatus, SourceManifest

class TestPhase6_5IntegrationAndConcurrency(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.keys_path = os.path.join(self.test_dir.name, "keys", "key_states.json")
        self.registry_path = os.path.join(self.test_dir.name, "registry", "registry.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def _make_key_pool(self, cooldown_seconds=60):
        return KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=cooldown_seconds)

    def _make_registry(self):
        return ProjectRegistry(storage_path=self.registry_path)

    # -------------------------------------------------------------------------
    # 1. 10-Key Discovery 통합 테스트 (연속 및 비연속, bashrc 우선순위)
    # -------------------------------------------------------------------------
    def test_01_ten_key_discovery_contiguous(self):
        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            discovered = pool.discover_keys()
            self.assertEqual(discovered, list(range(1, 11)))

            status = pool.get_status()
            self.assertEqual(len(status), 10)
            for i in range(1, 11):
                self.assertIn(f"key{i}", status)
                self.assertEqual(status[f"key{i}"]["state"], "ACTIVE")

    def test_02_ten_key_discovery_sparse(self):
        sparse_indices = [1, 3, 5, 7, 10]
        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in sparse_indices}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            discovered = pool.discover_keys()
            self.assertEqual(discovered, sparse_indices)

            status = pool.get_status()
            self.assertEqual(len(status), 5)
            for idx in sparse_indices:
                self.assertIn(f"key{idx}", status)

    def test_03_discovery_environ_overrides_bashrc(self):
        env = {"AGY_KEY_1": "env_secret_1"}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            discovered = pool.discover_keys()
            # Since environment contains keys, bashrc check is bypassed
            self.assertEqual(discovered, [1])

    # -------------------------------------------------------------------------
    # 2. 10-Key LRU Distribution (30회 acquire 수렴 및 특정 key 집중 방지)
    # -------------------------------------------------------------------------
    def test_04_ten_key_lru_distribution_30_acquisitions(self):
        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            used_sequence = []
            
            for _ in range(30):
                ref, idx = pool.acquire_key()
                used_sequence.append(ref)
                time.sleep(0.001)  # timestamp separation

            # First 10 should cycle key1..key10
            self.assertEqual(used_sequence[:10], [f"key{i}" for i in range(1, 11)])
            # Next 10 should cycle key1..key10
            self.assertEqual(used_sequence[10:20], [f"key{i}" for i in range(1, 11)])
            # Final 10 should cycle key1..key10
            self.assertEqual(used_sequence[20:30], [f"key{i}" for i in range(1, 11)])

            # Verify uniform distribution (each key used exactly 3 times)
            from collections import Counter
            counts = Counter(used_sequence)
            for i in range(1, 11):
                self.assertEqual(counts[f"key{i}"], 3)

    # -------------------------------------------------------------------------
    # 3. Multi-Project Sticky Binding (5개 프로젝트 매핑 및 상호 격리)
    # -------------------------------------------------------------------------
    @patch("requests.post")
    def test_05_multi_project_sticky_binding_five_projects(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_multi", "output": "ok"}
        mock_post.return_value = mock_resp

        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            registry = self._make_registry()

            # Create & register 5 projects
            project_ids = ["project-A", "project-B", "project-C", "project-D", "project-E"]
            clients = {}
            for idx, pid in enumerate(project_ids, start=1):
                pdir = os.path.join(self.test_dir.name, pid)
                os.makedirs(pdir, exist_ok=True)
                registry.register_project(pdir, project_id=pid)
                # Assign initial binding: A->key1, B->key2, C->key3, D->key4, E->key5
                registry.update_project_state(pid, active_key=f"key{idx}")
                clients[pid] = AntigravityClient(key_pool=pool, project_id=pid, registry=registry)

            # Perform 3 consecutive interactions per project
            for _ in range(3):
                for pid in project_ids:
                    res = clients[pid].create_interaction(prompt=f"req for {pid}")
                    self.assertTrue(res["success"])
                    expected_key = f"key{project_ids.index(pid) + 1}"
                    self.assertEqual(res["key_ref"], expected_key)

            # Re-verify registry bindings remained stable
            for idx, pid in enumerate(project_ids, start=1):
                self.assertEqual(registry.get_project(pid)["active_key"], f"key{idx}")

            # Re-bind project-A to key10 and check project-B..E remain unaffected
            registry.update_project_state("project-A", active_key="key10")
            res_a = clients["project-A"].create_interaction(prompt="req A key10")
            self.assertEqual(res_a["key_ref"], "key10")

            res_b = clients["project-B"].create_interaction(prompt="req B stay key2")
            self.assertEqual(res_b["key_ref"], "key2")

    # -------------------------------------------------------------------------
    # 4. Concurrent Multi-Project Requests (10 workers, 5 projects)
    # -------------------------------------------------------------------------
    @patch("requests.post")
    def test_06_concurrent_multi_project_requests(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_conc", "output": "ok"}
        mock_post.return_value = mock_resp

        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool()
            registry = self._make_registry()

            project_ids = [f"project-{p}" for p in ["A", "B", "C", "D", "E"]]
            for idx, pid in enumerate(project_ids, start=1):
                pdir = os.path.join(self.test_dir.name, pid)
                os.makedirs(pdir, exist_ok=True)
                registry.register_project(pdir, project_id=pid)
                registry.update_project_state(pid, active_key=f"key{idx}")

            def worker_task(task_id):
                pid = project_ids[task_id % len(project_ids)]
                client = AntigravityClient(key_pool=pool, project_id=pid, registry=registry)
                res = client.create_interaction(prompt=f"conc task {task_id}")
                return pid, res

            # Execute 20 total requests across 10 concurrent thread workers
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(worker_task, i) for i in range(20)]
                results = [f.result() for f in futures]

            for pid, res in results:
                self.assertTrue(res["success"])
                expected_key = f"key{project_ids.index(pid) + 1}"
                self.assertEqual(res["key_ref"], expected_key)

    # -------------------------------------------------------------------------
    # 5. Cascading Rollover across 10 Keys & Full Exhaustion
    # -------------------------------------------------------------------------
    @patch("requests.post")
    def test_07_cascading_rollover_ten_keys_and_full_exhaustion(self, mock_post):
        # 9 consecutive 429 failures, then 1 200 success on key10
        fail_responses = []
        for _ in range(9):
            r = MagicMock(status_code=429)
            r.json.return_value = {"error": "rate limit"}
            fail_responses.append(r)

        success_resp = MagicMock(status_code=200)
        success_resp.json.return_value = {"id": "int_key10", "output": "success on key10"}

        mock_post.side_effect = fail_responses + [success_resp]

        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool(cooldown_seconds=100)
            registry = self._make_registry()
            pdir = os.path.join(self.test_dir.name, "projA")
            os.makedirs(pdir, exist_ok=True)
            registry.register_project(pdir, project_id="project-A")
            registry.update_project_state("project-A", active_key="key1")

            client = AntigravityClient(key_pool=pool, project_id="project-A", registry=registry)
            res = client.create_interaction(prompt="cascade test")

            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key10")
            self.assertEqual(registry.get_project("project-A")["active_key"], "key10")

            status = pool.get_status()
            for i in range(1, 10):
                self.assertEqual(status[f"key{i}"]["state"], "COOLDOWN")
            self.assertEqual(status["key10"]["state"], "ACTIVE")

    @patch("requests.post")
    def test_08_full_exhaustion_all_10_keys(self, mock_post):
        # 10 consecutive 429 failures
        fail_responses = []
        for _ in range(10):
            r = MagicMock(status_code=429)
            r.json.return_value = {"error": "rate limit"}
            fail_responses.append(r)

        mock_post.side_effect = fail_responses

        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool(cooldown_seconds=100)
            client = AntigravityClient(key_pool=pool)
            res = client.create_interaction(prompt="exhaustion test")

            self.assertFalse(res["success"])
            self.assertEqual(res["status_code"], 429)

            status = pool.get_status()
            for i in range(1, 11):
                self.assertEqual(status[f"key{i}"]["state"], "COOLDOWN")

    # -------------------------------------------------------------------------
    # 6. Cooldown Expiry under High Concurrency
    # -------------------------------------------------------------------------
    @patch("requests.post")
    def test_09_cooldown_expiry_recovery_under_concurrency(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "int_recovery", "output": "ok"}
        mock_post.return_value = mock_resp

        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            # Short cooldown of 1 second for fast test execution
            pool = self._make_key_pool(cooldown_seconds=1)
            
            # Put key1..key9 into COOLDOWN
            for i in range(1, 10):
                pool.report_result(f"key{i}", status_code=429)

            status_before = pool.get_status()
            self.assertEqual(status_before["key1"]["state"], "COOLDOWN")

            # Wait for cooldown expiration
            time.sleep(1.1)

            # Concurrent requests should successfully recover key1..key9 back to ACTIVE
            def acquire_task(task_id):
                ref, idx = pool.acquire_key()
                return ref

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(acquire_task, i) for i in range(10)]
                acquired_keys = [f.result() for f in futures]

            self.assertEqual(len(acquired_keys), 10)
            # Verify key1 is recovered and active
            status_after = pool.get_status()
            self.assertEqual(status_after["key1"]["state"], "ACTIVE")

    # -------------------------------------------------------------------------
    # 7. WorkspaceSync Integration with 10 Keys & Rollover
    # -------------------------------------------------------------------------
    @patch("requests.get")
    def test_10_workspace_sync_integration_with_ten_keys_and_rollover(self, mock_get):
        # 429 on key1, then 200 on key2
        resp_429 = MagicMock(status_code=429, text="Rate limit")
        resp_429.json.return_value = {"error": {"message": "RESOURCE_EXHAUSTED"}}
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"files": []}

        mock_get.side_effect = [resp_429, resp_200]

        env = {f"AGY_KEY_{i}": f"dummy_secret_{i}" for i in range(1, 11)}
        with patch.dict(os.environ, env, clear=True):
            pool = self._make_key_pool(cooldown_seconds=60)
            registry = self._make_registry()

            pdir = os.path.join(self.test_dir.name, "projA")
            os.makedirs(pdir, exist_ok=True)
            registry.register_project(pdir, project_id="project-A")
            registry.update_project_state("project-A", active_key="key1")

            manifest = SourceManifest(
                source_type="local",
                source_path=pdir,
                files={},
                total_size=0
            )

            ws = WorkspaceSync(key_pool=pool, project_id="project-A", registry=registry)
            conflicts, err_type, err_msg = ws.check_remote_conflicts("env_1", manifest)

            self.assertEqual(conflicts, [])
            self.assertIsNone(err_type)

            status = pool.get_status()
            self.assertEqual(status["key1"]["state"], "COOLDOWN")
            self.assertEqual(status["key2"]["state"], "ACTIVE")
            self.assertEqual(registry.get_project("project-A")["active_key"], "key2")


if __name__ == "__main__":
    unittest.main()
