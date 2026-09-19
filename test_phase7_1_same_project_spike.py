"""
Live Spike: Test whether two keys from the same Google Cloud project can share
environments/interactions, versus cross-project keys.
"""
import os
import unittest
import requests
from keys import get_api_key_by_index

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS_URL = f"{API_BASE_URL}/interactions"
ENVIRONMENTS_URL = f"{API_BASE_URL}/environments"
AGENT_NAME = "antigravity-preview-09-2026"

class TestSameProjectLiveCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k1 = get_api_key_by_index(1)
        cls.k2 = get_api_key_by_index(2)
        if not cls.k1 or not cls.k2:
            raise unittest.SkipTest("Both key1 and key2 are required.")

    def test_same_project_or_cross_project_behavior(self):
        h1 = {"Content-Type": "application/json", "x-goog-api-key": self.k1}
        h2 = {"Content-Type": "application/json", "x-goog-api-key": self.k2}

        # Step 1: Create environment and baseline interaction with key 1
        p1 = {
            "agent": AGENT_NAME,
            "input": "Remember the word: PHOENIX. Reply: READY",
            "environment": "remote"
        }
        r1 = requests.post(INTERACTIONS_URL, headers=h1, json=p1, timeout=40)
        self.assertEqual(r1.status_code, 200)
        env_id = r1.json().get("environment_id")
        int_id = r1.json().get("id")
        self.assertTrue(env_id)
        self.assertTrue(int_id)

        # Step 2: Attempt access with key 2
        r_env = requests.get(f"{ENVIRONMENTS_URL}/{env_id}", headers=h2, timeout=20)
        p_cont = {
            "agent": AGENT_NAME,
            "input": "What word did I ask you to remember? Reply with only the word.",
            "environment": "remote",
            "environment_id": env_id,
            "previous_interaction_id": int_id
        }
        r_cont = requests.post(INTERACTIONS_URL, headers=h2, json=p_cont, timeout=40)

        # Record findings
        is_same_project = (r_env.status_code == 200 and r_cont.status_code == 200)
        print(f"\n[SPIKE RESULT] Key1 -> Key2 Environment Access Status: {r_env.status_code}")
        print(f"[SPIKE RESULT] Key1 -> Key2 Interaction Continuation Status: {r_cont.status_code}")
        print(f"[SPIKE RESULT] Same-Project Sharing Verdict: {'SHARED (SAME PROJECT)' if is_same_project else 'ISOLATED (CROSS PROJECT / TENANT LOCKED)'}")

        if not is_same_project:
            self.assertEqual(r_env.status_code, 404)
            self.assertEqual(r_cont.status_code, 404)

if __name__ == "__main__":
    unittest.main()
