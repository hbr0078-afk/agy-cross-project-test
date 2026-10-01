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
        session_tenant = ""

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
                    session_tenant = target_session.get("tenant_id", "")
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
                    # Update session but preserve tenant if it was already set
                    # or set it from key if possible (though in single-key mode we might not know)
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
            session_tenant=session_tenant
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
        session_tenant: str = "",
    ) -> Dict[str, Any]:
        """
        Manages key-pool lifecycle:
        - Acquires an active key from KeyPoolManager
        - Handles same-tenant rollover and cross-tenant fallback
        - Prevents cross-tenant 404s by checking tenant metadata
        - Recovers from interaction (404) or environment (404) loss
        - Reports outcome to KeyPoolManager
        """
        discovered_keys = self._key_pool.discover_keys()
        total_keys = len(discovered_keys) if discovered_keys else 1
        max_attempts = max(total_keys * 2, 4)

        tried_keys = set()
        same_key_retried = set()
        excluded_keys = set()
        attempts = 0
        last_result = None

        current_env_id = environment_id
        current_prev_id = previous_interaction_id
        current_tenant = session_tenant

        while attempts < max_attempts:
            attempts += 1
            try:
                # 1. Selection Strategy: Prioritize same-tenant keys if we have a session_tenant
                prefer_candidate = None
                if project_id and self.registry:
                    try:
                        p = self.registry.get_project(project_id)
                        if p and p.get("active_key") and p["active_key"] not in excluded_keys:
                            prefer_candidate = p["active_key"]
                    except Exception: pass

                if not prefer_candidate:
                    status_data = self._key_pool.get_status()
                    active_candidates = [k for k, v in status_data.items() if v.get("state") == "ACTIVE" and k not in excluded_keys]
                    
                    if current_tenant:
                        same_tenant = [k for k in active_candidates if status_data[k].get("tenant_id") == current_tenant]
                        if same_tenant:
                            prefer_candidate = same_tenant[0]
                    
                    if not prefer_candidate and active_candidates:
                        prefer_candidate = active_candidates[0]

                key_ref, key_idx = self._key_pool.acquire_key(
                    prefer_key=prefer_candidate,
                    project_id=project_id,
                    registry=self.registry
                )
            except AllKeysExhaustedError:
                if last_result is not None: return last_result
                raise

            tried_keys.add(key_ref)
            raw_key = get_api_key_by_index(key_idx)
            if not raw_key:
                self._key_pool.report_result(key_ref, status_code=401)
                excluded_keys.add(key_ref)
                continue

            # 2. Cross-Tenant 404 Prevention
            chosen_tenant = self._key_pool.get_key_tenant(key_ref)
            if current_tenant and chosen_tenant and current_tenant != chosen_tenant:
                # Tenant mismatch detected before API call -> clear environment & interaction context
                current_env_id = None
                current_prev_id = None
                current_tenant = chosen_tenant
                if session_id and self.session_manager:
                    self.session_manager.invalidate_session(session_id)
            elif not current_tenant and chosen_tenant:
                current_tenant = chosen_tenant

            # 3. Execute Request
            result = self._execute_request(
                api_key=raw_key,
                prompt=prompt,
                environment=environment,
                environment_id=current_env_id,
                previous_interaction_id=current_prev_id,
                timeout=timeout,
            )
            result["key_ref"] = key_ref
            result["key_index"] = key_idx
            last_result = result
            status_code = result.get("status_code")

            # 4. Handle Success
            if result.get("success") and status_code == 200:
                self._key_pool.report_result(key_ref, status_code=200)
                new_env_id = result.get("environment_id")
                interaction_id = result.get("interaction_id")
                
                # Update session/registry state BEFORE Sync (Phase 7-4 fix)
                # This ensures WorkspaceSync uses the new key/tenant context.
                if session_id and self.session_manager:
                    try:
                        self.session_manager.update_session(
                            session_id=session_id,
                            bound_key=key_ref,
                            tenant_id=chosen_tenant,
                            environment_id=new_env_id,
                            last_interaction_id=interaction_id,
                            state="ACTIVE"
                        )
                    except Exception: pass
                # In session mode, SessionStateManager is the single source of truth for runtime state.
                # ProjectRegistry must NOT be updated with runtime session state.

                # If environment changed (e.g. fresh environment provisioned due to fallback)
                if new_env_id and new_env_id != environment_id and project_id and self.registry:
                    self._sync_project_to_environment(new_env_id, project_id, session_id)

                return result

            # 5. Handle 404 (Environment or Interaction missing)
            if status_code == 404:
                if current_prev_id:
                    # Case A: Interaction lost but environment might be fine -> reset interaction_id and retry
                    current_prev_id = None
                    attempts -= 1
                    continue
                else:
                    # Case B: Environment lost or Cross-Tenant 은폐형 404 -> reset environment_id and retry fresh
                    if session_id and self.session_manager:
                        self.session_manager.invalidate_session(session_id)
                    current_env_id = None
                    current_tenant = chosen_tenant
                    attempts -= 1
                    continue

            # 6. Handle Rollover (429, 401, 403)
            if status_code in (429, 401, 403):
                self._key_pool.report_result(key_ref, status_code=status_code, error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # 7. Handle Transient Errors (5xx, Network)
            if status_code is None or status_code == 0 or status_code >= 500:
                if key_ref not in same_key_retried:
                    same_key_retried.add(key_ref)
                    attempts -= 1
                    continue
                self._key_pool.report_result(key_ref, status_code=status_code)
                excluded_keys.add(key_ref)
                continue

            # Default client error
            self._key_pool.report_result(key_ref, status_code=status_code)
            return result

        return last_result or {"success": False, "error": "Max attempts reached"}

    def _sync_project_to_environment(self, environment_id: str, project_id: str, session_id: Optional[str]):
        """Runs WorkspaceSync to populate project files in a new environment."""
        proj = self.registry.get_project(project_id) if self.registry else None
        if proj and proj.get("path"):
            from workspace_sync import WorkspaceSync
            ws = WorkspaceSync(
                key_pool=self._key_pool,
                project_id=project_id,
                registry=self.registry,
                session_manager=self.session_manager,
                session_id=session_id
            )
            manifest, err = ws.prepare_source_from_local(proj["path"])
            if manifest:
                ws.sync_to_remote(environment_id, manifest, overwrite=True)
