"""
Phase 8-2 deterministic tests: request-level round-robin routing + local workspace.

No real API calls: requests.post is always mocked, and every assertion checks the
ACTUAL request body / selected project (never just the selector function).
"""
import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor

from keys import KeyPoolManager, AllKeysExhaustedError
from sessions import SessionStateManager
from round_robin import RoundRobinSelector, RequestRoundRobinRunner
from client import AntigravityClient
from local_workspace import LocalWorkspace, WorkspaceError


def make_env(tenants):
    """tenant entry None -> key with no tenant; else tenant id."""
    env = {}
    for i, tenant in enumerate(tenants, start=1):
        env[f"AGY_KEY_{i}"] = f"secret-{i}"
        if tenant:
            env[f"AGY_TENANT_{i}"] = tenant
    return env


def resp_ok(output="ok", iid="int_x", env_id=None, usage=None):
    r = MagicMock()
    r.status_code = 200
    body = {"id": iid, "status": "COMPLETED", "output": output}
    if env_id:
        body["environment_id"] = env_id
    if usage:
        body["usage"] = usage
    r.json.return_value = body
    return r


def resp_err(code, msg="err"):
    r = MagicMock()
    r.status_code = code
    r.json.return_value = {"error": msg}
    return r


def used_keys(mock_post):
    return [c[1]["headers"]["x-goog-api-key"] for c in mock_post.call_args_list]


class TestPhase8_2RequestRoundRobin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys_path = os.path.join(self.tmp.name, "key_states.json")
        self.rr_path = os.path.join(self.tmp.name, "rr_state.json")
        self.sessions_path = os.path.join(self.tmp.name, "sessions.json")
        self.ws_root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.ws_root, exist_ok=True)

    def _pool(self, cooldown_seconds=60):
        return KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=cooldown_seconds)

    def _client(self, pool, **kw):
        sel = RoundRobinSelector(key_pool=pool, storage_path=self.rr_path)
        return AntigravityClient(key_pool=pool, round_robin=sel, **kw), sel

    # 1. A -> B -> C -> A cycle
    @patch("client.requests.post")
    def test_01_cycle_a_b_c_a(self, mock_post):
        mock_post.return_value = resp_ok()
        with patch.dict(os.environ, make_env(["A", "B", "C"]), clear=True):
            pool = self._pool()
            client, sel = self._client(pool)
            for i in range(4):
                res = client.create_interaction(prompt=f"p{i}")
                self.assertTrue(res["success"])
        self.assertEqual(used_keys(mock_post), ["secret-1", "secret-2", "secret-3", "secret-1"])
        counts = sel.get_counts()
        self.assertEqual(counts["A"]["requests"], 2)
        self.assertEqual(counts["B"]["requests"], 1)
        self.assertEqual(counts["C"]["requests"], 1)

    # 2. 10-project full rotation order
    @patch("client.requests.post")
    def test_02_ten_project_rotation(self, mock_post):
        mock_post.return_value = resp_ok()
        tenants = list("ABCDEFGHIJ")
        with patch.dict(os.environ, make_env(tenants), clear=True):
            pool = self._pool()
            client, _ = self._client(pool)
            for i in range(11):
                client.create_interaction(prompt=f"p{i}")
        expected = [f"secret-{i}" for i in range(1, 11)] + ["secret-1"]
        self.assertEqual(used_keys(mock_post), expected)

    # 3. no duplicate quota for multiple keys in the same project
    @patch("client.requests.post")
    def test_03_same_project_is_one_quota_unit(self, mock_post):
        mock_post.return_value = resp_ok()
        with patch.dict(os.environ, make_env(["A", "A", "B"]), clear=True):
            pool = self._pool()
            client, sel = self._client(pool)
            for i in range(4):
                client.create_interaction(prompt=f"p{i}")
        # project A owns key1+key2 -> one unit; key2 must never be a separate unit.
        self.assertEqual(used_keys(mock_post), ["secret-1", "secret-3", "secret-1", "secret-3"])
        self.assertEqual(sorted(sel.get_counts()), ["A", "B"])

    # 4. skip disabled + cooldown projects
    @patch("client.requests.post")
    def test_04_skip_disabled_and_cooldown(self, mock_post):
        mock_post.return_value = resp_ok()
        with patch.dict(os.environ, make_env(["A", "B", "C"]), clear=True):
            pool = self._pool()
            pool.report_result("key2", status_code=429)  # project B in COOLDOWN
            client, sel = self._client(pool)
            for i in range(3):
                client.create_interaction(prompt=f"p{i}")
            self.assertEqual(used_keys(mock_post), ["secret-1", "secret-3", "secret-1"])
            # explicitly disabled project is dropped too
            sel.disabled_projects = {"C"}
            mock_post.reset_mock()
            client.create_interaction(prompt="p3")
            self.assertEqual(used_keys(mock_post), ["secret-1"])
            self.assertEqual(sorted(sel.get_counts()), ["A", "C"])  # B never scheduled

    # 5. session bound_key does not interfere when RR is active
    @patch("client.requests.post")
    def test_05_session_bound_key_ignored(self, mock_post):
        mock_post.return_value = resp_ok()
        with patch.dict(os.environ, make_env(["A", "B", "C"]), clear=True):
            pool = self._pool()
            sm = SessionStateManager(storage_path=self.sessions_path)
            sm.create_session(project_id="proj", session_id="s1", bound_key="key3",
                              environment_id="env_legacy", tenant_id="C")
            sm.update_session(session_id="s1", last_interaction_id="int_legacy")
            client, _ = self._client(pool, session_manager=sm)
            res = client.create_interaction(prompt="p", session_id="s1", project_id="proj")
            self.assertTrue(res["success"])
        self.assertEqual(used_keys(mock_post), ["secret-1"])  # starts at A, not bound key3

    # 6. legacy routing behavior preserved when RR is disabled
    @patch("client.requests.post")
    def test_06_legacy_mode_unchanged(self, mock_post):
        mock_post.return_value = resp_ok()
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            pool = self._pool()
            client = AntigravityClient(key_pool=pool)
            self.assertFalse(client.is_round_robin)
            res = client.create_interaction(
                prompt="legacy", environment_id="env_1",
                previous_interaction_id="int_1", project_id="proj")
            self.assertTrue(res["success"])
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["environment_id"], "env_1")
        self.assertEqual(payload["previous_interaction_id"], "int_1")

    # 7. no cross-tenant environment_id / previous_interaction_id passed in RR mode
    @patch("client.requests.post")
    def test_07_no_remote_ids_in_rr_mode(self, mock_post):
        mock_post.return_value = resp_ok(env_id="env_from_api", iid="int_from_api")
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            pool = self._pool()
            sm = SessionStateManager(storage_path=self.sessions_path)
            sm.create_session(project_id="proj", session_id="s1",
                              environment_id="env_legacy", tenant_id="A")
            sm.update_session(session_id="s1", last_interaction_id="int_legacy")
            client, _ = self._client(pool, session_manager=sm)
            client.create_interaction(
                prompt="p", session_id="s1",
                environment_id="env_explicit", previous_interaction_id="int_explicit")
        payload = mock_post.call_args[1]["json"]
        self.assertNotIn("environment_id", payload)
        self.assertNotIn("previous_interaction_id", payload)

    # 8 + 9. file changes flow across requests (key1 -> key2 -> key3)
    @patch("client.requests.post")
    def test_08_09_file_state_flows_across_requests(self, mock_post):
        with open(os.path.join(self.ws_root, "a.py"), "w") as f:
            f.write("x=0\n")

        outputs = [
            json.dumps({"files": {"a.py": "x=1\n"}}),
            json.dumps({"files": {"b.py": "y=2\n"}}),
            json.dumps({"files": {"a.py": "x=3\n"}}),
        ]
        mock_post.side_effect = [resp_ok(o) for o in outputs]

        with patch.dict(os.environ, make_env(["A", "B", "C"]), clear=True):
            pool = self._pool()
            client, _ = self._client(pool)
            ws = LocalWorkspace(self.ws_root)
            runner = RequestRoundRobinRunner(client, ws)

            r1 = runner.run_request("step1", required_files=["a.py"])
            r2 = runner.run_request("step2", required_files=["a.py", "b.py"])
            r3 = runner.run_request("step3", required_files=["a.py", "b.py"])

        self.assertEqual(r1["applied"], ["a.py"])
        self.assertEqual(r2["applied"], ["b.py"])
        self.assertEqual(r3["applied"], ["a.py"])
        # selected projects follow the rotation
        self.assertEqual(used_keys(mock_post), ["secret-1", "secret-2", "secret-3"])
        # 8: key1's change is in key2's request body
        self.assertIn("x=1", mock_post.call_args_list[1][1]["json"]["input"])
        # 9: key2's change flows into key3's request body
        self.assertIn("y=2", mock_post.call_args_list[2][1]["json"]["input"])
        with open(os.path.join(self.ws_root, "a.py")) as f:
            self.assertEqual(f.read(), "x=3\n")

    # 10. invalid model output / collision / traversal never corrupts files
    @patch("client.requests.post")
    def test_10_invalid_output_preserves_files(self, mock_post):
        target = os.path.join(self.ws_root, "a.py")
        with open(target, "w") as f:
            f.write("original=1\n")

        mock_post.side_effect = [
            resp_ok("not json at all"),                                   # malformed
            resp_ok(json.dumps({"files": {"a.py": "def broken(:\n"}})),    # syntax error
            resp_ok(json.dumps({"files": {"../evil.py": "v=1\n"}})),       # traversal
        ]
        with patch.dict(os.environ, make_env(["A", "B", "C"]), clear=True):
            pool = self._pool()
            client, _ = self._client(pool)
            ws = LocalWorkspace(self.ws_root)
            runner = RequestRoundRobinRunner(client, ws)
            for _ in range(3):
                out = runner.run_request("bad", required_files=["a.py"])
                self.assertEqual(out["applied"], [])
                self.assertIn("validation_error", out)

        with open(target) as f:
            self.assertEqual(f.read(), "original=1\n")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.py")))

    # 11. concurrent requests keep pointer + file state safe
    @patch("client.requests.post")
    def test_11_concurrent_pointer_and_files(self, mock_post):
        mock_post.return_value = resp_ok()
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            pool = self._pool()
            client, sel = self._client(pool)
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(lambda i: client.create_interaction(prompt=f"p{i}"), range(20)))
            counts = sel.get_counts()
            self.assertEqual(sum(c["requests"] for c in counts.values()), 20)
            self.assertEqual(sorted(counts), ["A", "B"])
            with open(self.rr_path) as f:
                state = json.load(f)          # pointer file must stay valid JSON
            self.assertIn(state["last_project"], ("A", "B"))

        # concurrent writes to the SAME file must not corrupt it
        ws = LocalWorkspace(self.ws_root)
        contents = {f"v={i}\n" for i in range(8)}

        def writer(i):
            ws.apply_changes({"shared.py": f"v={i}\n"})

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(writer, range(8)))
        with open(os.path.join(self.ws_root, "shared.py")) as f:
            self.assertIn(f.read(), contents)

    # 12. 429 -> cooldown + limited retry, retry not counted as a new request
    @patch("client.requests.post")
    def test_12_429_cooldown_and_limited_retry(self, mock_post):
        mock_post.side_effect = [resp_err(429, "quota"), resp_ok("recovered"), resp_ok("next")]
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            pool = self._pool()
            client, sel = self._client(pool)
            res = client.create_interaction(prompt="p1")
            self.assertTrue(res["success"])
            self.assertEqual(res["key_ref"], "key2")
            self.assertEqual(pool.get_status()["key1"]["state"], "COOLDOWN")
            self.assertEqual(mock_post.call_count, 2)          # one schedule + one retry
            self.assertEqual(sel.get_counts()["A"]["requests"], 1)
            # next top-level request skips the cooled-down project A
            res2 = client.create_interaction(prompt="p2")
            self.assertEqual(res2["key_ref"], "key2")
        self.assertEqual([c[1]["headers"]["x-goog-api-key"] for c in mock_post.call_args_list],
                         ["secret-1", "secret-2", "secret-2"])

    @patch("client.requests.post")
    def test_12b_all_429_bounded(self, mock_post):
        mock_post.return_value = resp_err(429, "quota")
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            pool = self._pool()
            client, _ = self._client(pool)
            res = client.create_interaction(prompt="p1")
            self.assertFalse(res["success"])
            self.assertLessEqual(mock_post.call_count, 5)

    # extra: selector only enabled by explicit config
    def test_13_selector_gated_by_config(self):
        from config import RouterConfig
        from round_robin import selector_from_config

        class _Pool:
            def get_status(self):
                return {}

        self.assertIsNone(selector_from_config(RouterConfig(), _Pool()))
        self.assertIsNotNone(selector_from_config(
            RouterConfig(request_round_robin=True), _Pool(), storage_path=self.rr_path))

    def test_14_no_eligible_projects_raises(self):
        with patch.dict(os.environ, {"HOME": self.tmp.name}, clear=True):
            pool = self._pool()
            sel = RoundRobinSelector(key_pool=pool, storage_path=self.rr_path)
            with self.assertRaises(AllKeysExhaustedError):
                sel.next_project()

    def test_15_usage_estimation_labelled(self):
        with patch.dict(os.environ, make_env(["A"]), clear=True):
            pool = self._pool()
            sel = RoundRobinSelector(key_pool=pool, storage_path=self.rr_path)
            sel.record_usage("A", usage={"total_token_count": 42})
            self.assertFalse(sel.get_counts()["A"]["tokens_estimated"])
            sel.record_usage("A", usage=None, prompt_chars=8, output_chars=8)
        entry = sel.get_counts()["A"]
        self.assertEqual(entry["tokens"], 42 + 4)
        self.assertTrue(entry["tokens_estimated"])

    def test_16_workspace_path_safety_and_secrets(self):
        ws = LocalWorkspace(self.ws_root)
        with self.assertRaises(WorkspaceError):
            ws.safe_path("../escape.py")
        with self.assertRaises(WorkspaceError):
            ws.safe_path("/etc/passwd")
        with open(os.path.join(self.ws_root, ".env"), "w") as f:
            f.write("AGY_KEY_1=leak-me")
        ctx = ws.collect_context(required_files=[".env"], task_context="t")
        self.assertNotIn(".env", ctx["files"])
        with self.assertRaises(WorkspaceError):
            ws.parse_changes(json.dumps({"files": {".env": "x=1\n"}}))


if __name__ == "__main__":
    unittest.main()
