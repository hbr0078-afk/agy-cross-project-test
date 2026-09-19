import json
import requests
from typing import Dict, Any, Optional
from keys import KeyPoolManager, AllKeysExhaustedError, get_api_key_by_index

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
AGENT_NAME = "antigravity-preview-05-2026"

class AntigravityClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        key_pool: Optional[KeyPoolManager] = None,
        project_id: Optional[str] = None,
        registry: Optional[Any] = None,
        session_manager: Optional[Any] = None,
    ):
        if not api_key and key_pool is None:
            raise ValueError("Either api_key or key_pool must be provided.")
        self._api_key = api_key
        self._key_pool = key_pool
        self.project_id = project_id
        self.registry = registry
        self.session_manager = session_manager

    @property
    def is_pool_mode(self) -> bool:
        return self._key_pool is not None

    def create_interaction(
        self,
        prompt: str,
        environment: str = "remote",
        environment_id: Optional[str] = None,
        previous_interaction_id: Optional[str] = None,
        timeout: int = 120,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates an interaction with Antigravity Agent.
        In single-key mode: executes a direct request with the fixed API key.
        In key-pool mode: automatically acquires an active key, handles retries/rollovers
        on 429/401/403/5xx/network errors, updates key states, and returns the result.
        Supports session_id isolation via SessionStateManager.
        """
        target_project_id = project_id or self.project_id

        # Resolve session state if session_id or session_manager provided
        resolved_env_id = environment_id
        resolved_prev_id = previous_interaction_id
        target_session = None

        if self.session_manager and (session_id or target_project_id):
            sid = session_id or (f"sess_{target_project_id}" if target_project_id else None)
            if sid:
                target_session = self.session_manager.get_session(sid)
                if not target_session and target_project_id:
                    target_session = self.session_manager.create_session(
                        project_id=target_project_id,
                        session_id=sid,
                        environment_id=environment_id
                    )
                if target_session:
                    if not resolved_env_id and target_session.get("environment_id"):
                        resolved_env_id = target_session["environment_id"]
                    if not resolved_prev_id and target_session.get("last_interaction_id"):
                        resolved_prev_id = target_session["last_interaction_id"]

        if not self.is_pool_mode:
            res = self._execute_request(
                api_key=self._api_key,
                prompt=prompt,
                environment=environment,
                environment_id=resolved_env_id,
                previous_interaction_id=resolved_prev_id,
                timeout=timeout,
            )
            if res.get("success") and target_session and self.session_manager:
                try:
                    self.session_manager.update_session(
                        session_id=target_session["session_id"],
                        environment_id=res.get("environment_id"),
                        last_interaction_id=res.get("interaction_id"),
                        state="ACTIVE"
                    )
                except Exception:
                    pass
            return res

        return self._create_interaction_with_pool(
            prompt=prompt,
            environment=environment,
            environment_id=resolved_env_id,
            previous_interaction_id=resolved_prev_id,
            timeout=timeout,
            project_id=target_project_id,
            session_id=target_session.get("session_id") if target_session else session_id,
        )

    def _execute_request(
        self,
        api_key: str,
        prompt: str,
        environment: str,
        environment_id: Optional[str],
        previous_interaction_id: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
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

        try:
            response = requests.post(API_BASE_URL, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "status_code": None,
                "error": {"network_error": str(e)},
            }

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

    def _create_interaction_with_pool(
        self,
        prompt: str,
        environment: str,
        environment_id: Optional[str],
        previous_interaction_id: Optional[str],
        timeout: int,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Manages key-pool lifecycle:
        - Acquires an active key from KeyPoolManager
        - Reports outcome to KeyPoolManager (200, 429, 401, 403, 5xx, timeout/network)
        - Retries on same key once for 5xx / network errors
        - Rotates to next available key on 429, 401, 403, and exhausted 5xx/network errors
        - Immediately stops on 4xx client errors (400, 404, etc.) without rotation
        - Stops if all keys are exhausted (raises AllKeysExhaustedError) or retry limit reached
        """
        discovered_keys = self._key_pool.discover_keys()
        total_keys = len(discovered_keys) if discovered_keys else 1
        # Safe retry bound: allow up to total_keys rotations, with 1 retry per key for transient errors
        max_attempts = max(total_keys * 2, 4)

        tried_keys = set()
        same_key_retried = set()
        excluded_keys = set()
        attempts = 0
        last_result = None

        while attempts < max_attempts:
            attempts += 1
            try:
                # Prioritize project_id's active_key from registry if project_id is provided
                prefer_candidate = None
                if project_id and self.registry:
                    try:
                        p = self.registry.get_project(project_id)
                        if p and p.get("active_key") and p["active_key"] not in excluded_keys:
                            prefer_candidate = p["active_key"]
                    except Exception:
                        pass

                if not prefer_candidate:
                    data = self._key_pool.get_status()
                    candidates = [
                        k for k, v in data.items()
                        if v.get("state") == "ACTIVE" and k not in excluded_keys
                    ]
                    prefer_candidate = candidates[0] if candidates else None

                key_ref, key_idx = self._key_pool.acquire_key(
                    prefer_key=prefer_candidate,
                    project_id=project_id,
                    registry=self.registry
                )

                if key_ref in excluded_keys:
                    # If acquired key was previously excluded during this transaction, acquire next available
                    data = self._key_pool.get_status()
                    candidates = [
                        k for k, v in data.items()
                        if v.get("state") == "ACTIVE" and k not in excluded_keys
                    ]
                    if not candidates:
                        raise AllKeysExhaustedError("All non-excluded active keys exhausted.")
                    key_ref, key_idx = self._key_pool.acquire_key(prefer_key=candidates[0])
            except AllKeysExhaustedError:
                if last_result is not None:
                    return last_result
                raise

            tried_keys.add(key_ref)
            raw_key = get_api_key_by_index(key_idx)
            if not raw_key:
                # Key index found in discovery but raw key not readable -> mark INACTIVE
                self._key_pool.report_result(key_ref, status_code=401)
                excluded_keys.add(key_ref)
                continue

            result = self._execute_request(
                api_key=raw_key,
                prompt=prompt,
                environment=environment,
                environment_id=environment_id,
                previous_interaction_id=previous_interaction_id,
                timeout=timeout,
            )
            result["key_ref"] = key_ref
            result["key_index"] = key_idx
            last_result = result

            status_code = result.get("status_code")

            # 1. Success (200)
            if result.get("success") and status_code == 200:
                self._key_pool.report_result(key_ref, status_code=200)
                if session_id and self.session_manager:
                    # Session mode: update runtime state ONLY in SessionStateManager
                    try:
                        self.session_manager.update_session(
                            session_id=session_id,
                            bound_key=key_ref,
                            environment_id=result.get("environment_id"),
                            last_interaction_id=result.get("interaction_id"),
                            state="ACTIVE"
                        )
                    except Exception:
                        pass
                else:
                    # Non-session mode: update project active_key in registry if project_id is provided
                    if project_id and self.registry:
                        try:
                            self.registry.update_project_state(
                                project_id=project_id,
                                active_key=key_ref,
                                environment_id=result.get("environment_id"),
                                last_interaction_id=result.get("interaction_id")
                            )
                        except Exception:
                            pass
                return result

            # 2. Client error (400, 404, etc. excluding 401, 403, 429) -> Do NOT rotate
            if status_code is not None and 400 <= status_code < 500 and status_code not in (401, 403, 429):
                self._key_pool.report_result(key_ref, status_code=status_code, error_data=result.get("error"))
                return result

            # 3. 429 (Rate limit) -> Cooldown & Rotate to next key immediately
            if status_code == 429:
                self._key_pool.report_result(key_ref, status_code=429, error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # 4. 401 (Invalid auth) -> Inactive & Rotate to next key immediately
            if status_code == 401:
                self._key_pool.report_result(key_ref, status_code=401, error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # 5. 403 (Forbidden) -> Record failure via report_result & Rotate to next key
            if status_code == 403:
                self._key_pool.report_result(key_ref, status_code=403, error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # 6. 5xx (Server error) -> Retry same key once, then rotate
            if status_code is not None and status_code in (500, 502, 503, 504):
                if key_ref not in same_key_retried:
                    same_key_retried.add(key_ref)
                    # Retry same key once without switching
                    continue
                # Already retried same key once -> report to pool and rotate
                self._key_pool.report_result(key_ref, status_code=status_code, error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # 7. Network / Timeout error (status_code is None or 0) -> Retry same key once, then rotate
            if status_code is None or status_code == 0:
                if key_ref not in same_key_retried:
                    same_key_retried.add(key_ref)
                    # Retry same key once without switching
                    continue
                # Already retried same key once -> report to pool and rotate
                self._key_pool.report_result(key_ref, status_code=None, error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # Default fallback for unhandled codes
            self._key_pool.report_result(key_ref, status_code=status_code, error_data=result.get("error"))
            excluded_keys.add(key_ref)

        return last_result if last_result is not None else {
            "success": False,
            "status_code": None,
            "error": {"error": "Maximum rollover attempts reached"},
        }
