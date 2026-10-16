"""
Phase 8-2.1 deterministic tests: runtime wiring + project-identity (tenant) mapping.

No real API calls: requests.post is always mocked. These tests exercise the REAL
integration path (runtime.build_client / runtime.run_coding_request), not a
hand-built selector+runner, so they fail if the wiring is ever disconnected.
"""
import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from config import RouterConfig
from keys import KeyPoolManager
from client import AntigravityClient
from round_robin import RoundRobinSelector, UNKNOWN_PROJECT_ID
from local_workspace import LocalWorkspace
from runtime import (
    build_client, build_runner, run_coding_request, RuntimeWiringError,
)


def make_env(tenants):
    """tenant entry None -> key without tenant metadata."""
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


class _FakePool:
    """Minimal KeyPoolManager stand-in for pure project-grouping assertions."""

    def __init__(self, status):
        self._status = status

    def get_status(self):
        return self._status


class TestProjectIdentityMapping(unittest.TestCase):
    """Section 3: distinct projects are unique slots; same/unknown are not inflated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rr_path = os.path.join(self.tmp.name, "rr_state.json")

    def _selector(self, status, disabled=None):
        return RoundRobinSelector(_FakePool(status), storage_path=self.rr_path,
                                  disabled_projects=disabled)

    def test_missing_identity_is_one_slot(self):
        # Three keys, no tenant metadata -> a single unknown slot, never three units.
        status = {f"key{i}": {"index": i, "state": "ACTIVE", "tenant_id": ""}
                  for i in (1, 2, 3)}
        with patch("round_robin.get_api_key_by_index", return_value="secret"):
            projects = self._selector(status).eligible_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project_id"], UNKNOWN_PROJECT_ID)
        self.assertEqual(len(projects[0]["key_refs"]), 3)

    def test_duplicate_identity_is_one_unit(self):
        # key1+key2 share project A -> one unit; key3 (B) -> second unit.
        status = {
            "key1": {"index": 1, "state": "ACTIVE", "tenant_id": "A"},
            "key2": {"index": 2, "state": "ACTIVE", "tenant_id": "A"},
            "key3": {"index": 3, "state": "ACTIVE", "tenant_id": "B"},
        }
        with patch("round_robin.get_api_key_by_index", return_value="secret"):
            projects = self._selector(status).eligible_projects()
        self.assertEqual([p["project_id"] for p in projects], ["A", "B"])
        by_id = {p["project_id"]: p["key_refs"] for p in projects}
        self.assertEqual(by_id["A"], ["key1", "key2"])
        self.assertEqual(by_id["B"], ["key3"])

    def test_valid_distinct_identities_are_distinct_slots(self):
        status = {f"key{i}": {"index": i, "state": "ACTIVE", "tenant_id": t}
                  for i, t in enumerate(["A", "B", "C"], start=1)}
        with patch("round_robin.get_api_key_by_index", return_value="secret"):
            projects = self._selector(status).eligible_projects()
        self.assertEqual([p["project_id"] for p in projects], ["A", "B", "C"])

    def test_mixed_known_and_unknown_never_collide(self):
        # key1 (A), key2 (no tenant) -> A + one unknown slot, not two unknowns.
        status = {
            "key1": {"index": 1, "state": "ACTIVE", "tenant_id": "A"},
            "key2": {"index": 2, "state": "ACTIVE", "tenant_id": ""},
        }
        with patch("round_robin.get_api_key_by_index", return_value="secret"):
            projects = self._selector(status).eligible_projects()
        self.assertEqual(sorted(p["project_id"] for p in projects),
                         sorted(["A", UNKNOWN_PROJECT_ID]))

    def test_cooldown_unknown_group_not_counted_separately(self):
        # Even with several unknown keys, exhausting the group removes one unit.
        status = {f"key{i}": {"index": i, "state": "COOLDOWN", "tenant_id": ""}
                  for i in (1, 2, 3)}
        with patch("round_robin.get_api_key_by_index", return_value="secret"):
            projects = self._selector(status).eligible_projects()
        self.assertEqual(projects, [])


class TestRuntimeWiring(unittest.TestCase):
    """Section 2/4: the real runtime entry point builds the whole stack."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys_path = os.path.join(self.tmp.name, "key_states.json")
        self.rr_path = os.path.join(self.tmp.name, "rr_state.json")
        self.ws_root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.ws_root, exist_ok=True)

    def _pool(self):
        return KeyPoolManager(storage_path=self.keys_path, cooldown_seconds=60)

    # --- factory behaviour ---
    def test_build_client_legacy_when_off(self):
        client, pool, sel = build_client(RouterConfig(), key_pool=self._pool())
        self.assertIsNone(sel)
        self.assertFalse(client.is_round_robin)
        self.assertTrue(client.is_pool_mode)

    def test_build_client_round_robin_when_on(self):
        cfg = RouterConfig(request_round_robin=True)
        client, pool, sel = build_client(cfg, key_pool=self._pool(),
                                         rr_storage_path=self.rr_path)
        self.assertIsNotNone(sel)
        self.assertTrue(client.is_round_robin)

    def test_build_runner_links_selector_to_client(self):
        cfg = RouterConfig(request_round_robin=True)
        runner, client, pool, sel = build_runner(
            cfg, self.ws_root, key_pool=self._pool(), rr_storage_path=self.rr_path)
        self.assertIsNotNone(sel)
        # Runner must reuse the client's own selector, not a detached copy.
        self.assertIs(runner.selector, client.round_robin)
        self.assertIsInstance(runner.workspace, LocalWorkspace)

    def test_run_coding_request_requires_rr_enabled(self):
        with self.assertRaises(RuntimeWiringError):
            run_coding_request(RouterConfig(), self.ws_root, "do it",
                               key_pool=self._pool(), rr_storage_path=self.rr_path)

    # --- real integration path, mocked transport ---
    @patch("client.requests.post")
    def test_end_to_end_rotation_and_state_flow(self, mock_post):
        with open(os.path.join(self.ws_root, "a.py"), "w") as f:
            f.write("x=0\n")
        out_a = json.dumps({"files": {"a.py": "x=1\n"}})
        out_b = json.dumps({"files": {"b.py": "y=2\n"}})
        out_c = json.dumps({"files": {"a.py": "x=3\n"}})
        mock_post.side_effect = [resp_ok(out_a), resp_ok(out_b), resp_ok(out_c)]

        cfg = RouterConfig(request_round_robin=True)
        pool = self._pool()
        with patch.dict(os.environ, make_env(["A", "B", "C"]), clear=True):
            r1 = run_coding_request(cfg, self.ws_root, "step1",
                                    required_files=["a.py"], key_pool=pool,
                                    rr_storage_path=self.rr_path)
            r2 = run_coding_request(cfg, self.ws_root, "step2",
                                    required_files=["a.py", "b.py"], key_pool=pool,
                                    rr_storage_path=self.rr_path)
            r3 = run_coding_request(cfg, self.ws_root, "step3",
                                    required_files=["a.py", "b.py"], key_pool=pool,
                                    rr_storage_path=self.rr_path)

        # requests 1/2/3 select projects A/B/C
        self.assertEqual(used_keys(mock_post), ["secret-1", "secret-2", "secret-3"])
        self.assertEqual(r1["applied"], ["a.py"])
        self.assertEqual(r2["applied"], ["b.py"])
        self.assertEqual(r3["applied"], ["a.py"])
        # next request carries the latest validated local file state
        self.assertIn("x=1", mock_post.call_args_list[1][1]["json"]["input"])
        self.assertIn("y=2", mock_post.call_args_list[2][1]["json"]["input"])
        with open(os.path.join(self.ws_root, "a.py")) as f:
            self.assertEqual(f.read(), "x=3\n")

    @patch("client.requests.post")
    def test_end_to_end_no_remote_ids(self, mock_post):
        mock_post.return_value = resp_ok(json.dumps({"files": {"a.py": "x=1\n"}}),
                                         env_id="env_from_api")
        with open(os.path.join(self.ws_root, "a.py"), "w") as f:
            f.write("x=0\n")
        cfg = RouterConfig(request_round_robin=True)
        pool = self._pool()
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            run_coding_request(cfg, self.ws_root, "p", key_pool=pool,
                               rr_storage_path=self.rr_path)
        payload = mock_post.call_args[1]["json"]
        self.assertNotIn("environment_id", payload)
        self.assertNotIn("previous_interaction_id", payload)

    @patch("client.requests.post")
    def test_end_to_end_invalid_output_untouched(self, mock_post):
        target = os.path.join(self.ws_root, "a.py")
        with open(target, "w") as f:
            f.write("original=1\n")
        mock_post.side_effect = [
            resp_ok("not json at all"),
            resp_ok(json.dumps({"files": {"a.py": "def broken(:\n"}})),
        ]
        cfg = RouterConfig(request_round_robin=True)
        pool = self._pool()
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            for _ in range(2):
                out = run_coding_request(cfg, self.ws_root, "bad",
                                         required_files=["a.py"], key_pool=pool,
                                         rr_storage_path=self.rr_path)
                self.assertEqual(out["applied"], [])
                self.assertIn("validation_error", out)
        with open(target) as f:
            self.assertEqual(f.read(), "original=1\n")

    @patch("client.requests.post")
    def test_429_bounded_single_success(self, mock_post):
        # 429 on project A rolls over to B; exactly one successful op is counted.
        mock_post.side_effect = [
            resp_err(429, "quota"),
            resp_ok(json.dumps({"files": {"a.py": "x=9\n"}})),
        ]
        with open(os.path.join(self.ws_root, "a.py"), "w") as f:
            f.write("x=0\n")
        cfg = RouterConfig(request_round_robin=True)
        pool = self._pool()
        with patch.dict(os.environ, make_env(["A", "B"]), clear=True):
            out = run_coding_request(cfg, self.ws_root, "p",
                                     required_files=["a.py"], key_pool=pool,
                                     rr_storage_path=self.rr_path)
        self.assertEqual(out["applied"], ["a.py"])
        self.assertEqual(mock_post.call_count, 2)  # schedule + one retry, not more
        sel = RoundRobinSelector(key_pool=pool, storage_path=self.rr_path)
        counts = sel.get_counts()
        self.assertEqual(sum(c["requests"] for c in counts.values()), 1)


if __name__ == "__main__":
    unittest.main()
