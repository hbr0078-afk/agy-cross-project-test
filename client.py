import json
import requests
from typing import Dict, Any, Optional

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
AGENT_NAME = "antigravity-preview-05-2026"

class AntigravityClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("API key must not be empty.")
        self._api_key = api_key

    def create_interaction(
        self,
        prompt: str,
        environment: str = "remote",
        environment_id: Optional[str] = None,
        previous_interaction_id: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Creates an interaction with Antigravity Agent.
        """
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        payload: Dict[str, Any] = {
            "agent": AGENT_NAME,
            "input": prompt,
            "environment": environment,
        }
        if environment_id:
            payload["environment_id"] = environment_id
        if previous_interaction_id:
            payload["previous_interaction_id"] = previous_interaction_id

        response = requests.post(API_BASE_URL, headers=headers, json=payload, timeout=timeout)
        
        if response.status_code != 200:
            error_data = {}
            try:
                error_data = response.json()
            except Exception:
                error_data = {"raw_text": response.text}
            return {
                "success": False,
                "status_code": response.status_code,
                "error": error_data,
            }

        res_json = response.json()
        
        # Parse output safely
        output_text = ""
        if res_json.get("output"):
            output_text = str(res_json.get("output"))
        elif res_json.get("steps"):
            for step in res_json.get("steps", []):
                if step.get("content"):
                    for c in step.get("content", []):
                        if isinstance(c, dict) and c.get("text"):
                            output_text += c.get("text") + "\n"
                elif step.get("text"):
                    output_text += step.get("text") + "\n"

        return {
            "success": True,
            "status_code": 200,
            "status": res_json.get("status"),
            "environment_id": res_json.get("environment_id"),
            "interaction_id": res_json.get("id"),
            "usage": res_json.get("usage", {}),
            "output": output_text.strip(),
            "raw": res_json,
        }
