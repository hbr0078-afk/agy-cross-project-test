"""
Phase 7-1 Live Spike: Antigravity Resource Ownership & Tenant Continuity.
Verifies cross-key and cross-project boundaries for Environment and Interaction resources.
Strictly sanitized: no raw API secrets or sensitive headers are recorded or displayed.
"""
import os
import unittest
import requests
from keys import get_api_key_by_index

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS_URL = f"{API_BASE_URL}/interactions"
ENVIRONMENTS_URL = f"{API_BASE_URL}/environments"
AGENT_NAME = "antigravity-preview-09-2026"

class TestPhase7_1LiveSpike(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k1 = get_api_key_by_index(1)
        cls.k2 = get_api_key_by_index(2)
        if not cls.k1 or not cls.k2:
            raise unittest.SkipTest("Both key1 and key2 are required for Phase 7-1 live spike.")

    def test_01_agent_compatibility_05_and_09(self):
        h = {"Content-Type": "application/json", "x-goog-api-key": self.k1}
        for agent in ["antigravity-preview-05-2026", "antigravity-preview-09-2026"]:
            payload = {
                "agent": agent,
                "input": "Return exactly: SPIKE_AGENT_OK",
                "environment": "remote"
            }
            r = requests.post(INTERACTIONS_URL, headers=h, json=payload, timeout=40)
            self.assertEqual(r.status_code, 200, f"Agent {agent} returned status {r.status_code}")
            data = r.json()
            self.assertIn("environment_id", data)
            self.assertIn("id", data)

    def test_02_same_key_continuity(self):
        h = {"Content-Type": "application/json", "x-goog-api-key": self.k1}
        p1 = {
            "agent": AGENT_NAME,
            "input": "Remember the token: SPIKE_LIVE_771. Reply exactly: BASELINE_OK",
            "environment": "remote"
        }
        r1 = requests.post(INTERACTIONS_URL, headers=h, json=p1, timeout=40)
        self.assertEqual(r1.status_code, 200)
        env_id = r1.json().get("environment_id")
        int_id = r1.json().get("id")

        p2 = {
            "agent": AGENT_NAME,
            "input": "What token did I ask you to remember? Reply with only the token.",
            "environment": "remote",
            "environment_id": env_id,
            "previous_interaction_id": int_id
        }
        r2 = requests.post(INTERACTIONS_URL, headers=h, json=p2, timeout=40)
        self.assertEqual(r2.status_code, 200)
        text = ""
        for step in r2.json().get("steps", []):
            for c in step.get("content", []):
                text += c.get("text", "")
        self.assertIn("SPIKE_LIVE_771", text)

    def test_03_cross_key_cross_project_isolation(self):
        h1 = {"Content-Type": "application/json", "x-goog-api-key": self.k1}
        h2 = {"Content-Type": "application/json", "x-goog-api-key": self.k2}

        # 1. Create with Key 1
        p1 = {
            "agent": AGENT_NAME,
            "input": "Remember: SPIKE_ISOLATION_992",
            "environment": "remote"
        }
        r1 = requests.post(INTERACTIONS_URL, headers=h1, json=p1, timeout=40)
        self.assertEqual(r1.status_code, 200)
        env_1 = r1.json().get("environment_id")
        int_1 = r1.json().get("id")

        # 2. Key 2 GET environment -> must be 404 (strictly isolated)
        r_env2 = requests.get(f"{ENVIRONMENTS_URL}/{env_1}", headers=h2, timeout=20)
        self.assertEqual(r_env2.status_code, 404)

        # 3. Key 2 continue interaction -> must be 404
        p_cross = {
            "agent": AGENT_NAME,
            "input": "Recall token",
            "environment": "remote",
            "environment_id": env_1,
            "previous_interaction_id": int_1
        }
        r_cross = requests.post(INTERACTIONS_URL, headers=h2, json=p_cross, timeout=40)
        self.assertEqual(r_cross.status_code, 404)

if __name__ == "__main__":
    unittest.main()
