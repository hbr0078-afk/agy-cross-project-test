"""Simple wrapper to call Antigravity Cloud Interactions API directly.

This mirrors chi/achy-router's direct HTTP POST to
https://generativelanguage.googleapis.com/v1beta/interactions
so it can be used without the local harness / WebSocket flow.
"""

import requests
from typing import Any, Dict, Optional

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
AGENT_NAME = "antigravity-preview-09-2026"


def call_antigravity(
    prompt: Any,
    api_key: str,
    *,
    agent: Optional[str] = None,
    environment: str = "remote",
    environment_id: Optional[str] = None,
    previous_interaction_id: Optional[str] = None,
    model: Optional[str] = "gemini-4-argon",
    tools: Optional[list] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Call Antigravity Cloud API directly and return a normalized result dict.

    Args:
        prompt: User prompt text or function_result list.
        api_key: Google API key (x-goog-api-key).
        agent: Optional agent ID (e.g. antigravity-preview-09-2026).
        environment: Environment string sent to the API.
        environment_id: Optional environment/session ID for continuity.
        previous_interaction_id: Optional previous interaction ID to continue.
        model: Optional model ID to request (passed via agent_config).
        tools: Optional list of tool declarations.
        timeout: HTTP request timeout in seconds.
    """
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    payload: Dict[str, Any] = {
        "agent": agent or AGENT_NAME,
        "input": prompt,
        "environment": environment,
    }

    if environment_id:
        payload["environment_id"] = environment_id
    if previous_interaction_id:
        payload["previous_interaction_id"] = previous_interaction_id
    if model:
        payload["agent_config"] = {
            "type": "antigravity",
            "model": model,
        }
    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(
            API_BASE_URL,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "status_code": None,
            "error": {"network_error": str(e)},
        }

    if resp.status_code != 200:
        error_data: Any = {}
        try:
            error_data = resp.json()
        except Exception:
            error_data = {"raw_text": resp.text}
        return {
            "success": False,
            "status_code": resp.status_code,
            "error": error_data,
        }

    data = resp.json()

    output_text = ""
    if data.get("output"):
        output_text = str(data.get("output"))
    elif data.get("steps"):
        for step in data.get("steps", []):
            if step.get("content"):
                for c in step.get("content", []):
                    if isinstance(c, dict) and c.get("text"):
                        output_text += c.get("text") + "\n"
            elif step.get("text"):
                output_text += step.get("text") + "\n"

    return {
        "success": True,
        "status_code": resp.status_code,
        "status": data.get("status"),
        "environment_id": data.get("environment_id"),
        "interaction_id": data.get("id"),
        "usage": data.get("usage", {}),
        "output": output_text.strip(),
        "raw": data,
    }


if __name__ == "__main__":
    import os
    import sys

    key = (
        os.environ.get("AGY_KEY_1")
        or os.environ.get("GEMINI_API_KEY_1")
        or os.environ.get("GEMINI_API_KEY")
    )
    if not key:
        print("Set AGY_KEY_1 or GEMINI_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    result = call_antigravity("안녕하세요. 연결 테스트입니다. 짧은 인사말 한 줄만 해주세요.", key)
    if result["success"]:
        print(result["output"])
    else:
        print(f"FAILED: {result.get('error')}", file=sys.stderr)
        sys.exit(1)
