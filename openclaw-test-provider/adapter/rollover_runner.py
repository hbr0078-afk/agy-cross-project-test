"""Rollover retry runner for the Antigravity adapter.

Wires the ported KeyPoolManager onto the raw HTTP call so that errors are
classified and handled per policy instead of "blindly swap keys".

Policy (mirrors agy-router/client.py, trimmed to adapter needs):
  200          -> success, report ACTIVE
  429          -> report (COOLDOWN), rotate, retry
  401          -> report (INACTIVE), rotate, retry
  403          -> report, rotate, retry
  404          -> context reset once (interaction then environment), retry
  5xx / network-> same key retried ONCE, then rollover
  other 4xx    -> no penalty, return as-is (no retry)
  exhausted    -> return last error

Hard bounds:
  - max_attempts = max(discovered_keys * 2, 4)
  - each key excluded after it is rotated away
  - SAME-KEY transient retries are capped (one per key)

Tool-turn safety:
  A function_result is bound to the (call_id, environment, tenant) of the
  originating function_call. Rotating the key on a tool-result turn risks
  (a) cross-tenant 404 and (b) environment reset -> the model re-asks for a
  tool -> duplicate local execution. Therefore, when `tool_bound=True`, the
  runner does NOT rotate or reset context on failure; it returns an explicit
  error so the caller can surface it without pretending the tool succeeded.

Pinned key (`pinned_key_ref`):
  Google's Interactions API is "stateful only" for function calling
  (docs: "reconstructing history manually (stateless mode) is not supported").
  A tool chain therefore belongs to ONE tenant/key from its first function_call
  until the final text answer. While a chain is active the caller pins the key
  that emitted the call_id, and every subsequent turn (including each
  function_result submission) must reuse it. `run(pinned_key_ref=...)` then:
    - uses exactly that key (no round-robin, no rotation)
    - allows at most one same-key retry for 5xx/network
    - on failure returns `pin_failure=True` so the caller can drop the chain
      and restart the conversation on a fresh key
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from key_pool import KeyPoolManager, AllKeysExhaustedError, get_api_key_by_index


# errors that trigger a key change
_ROTATE_STATUS = (429, 401, 403)
# transient errors: one same-key retry, then rollover
_TRANSIENT_5XX = (500, 502, 503, 504)


def _dbg(event: Dict[str, Any]) -> None:
    import os
    import json
    if not os.environ.get("AGY_ADAPTER_DEBUG"):
        return
    try:
        event["ts"] = time.time()
        with open("/tmp/agy_adapter_rollover.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


class RolloverRunner:
    """Runs one logical interaction with key selection + classified retries."""

    def __init__(self, key_pool: KeyPoolManager):
        self.key_pool = key_pool

    def _run_pinned(
        self,
        execute: Callable[[str], Dict[str, Any]],
        key_ref: str,
    ) -> Dict[str, Any]:
        """Use exactly `key_ref` for one call. No rotation, no round-robin.

        One same-key retry is allowed for transient 5xx/network errors. Any
        failure is reported with `pin_failure=True` so the caller drops the
        chain (the call_id/environment are tenant-bound and cannot move keys).
        """
        status = self.key_pool.get_status()
        entry = status.get(key_ref)
        if not entry:
            _dbg({"event": "pinned_failure", "key_ref": key_ref,
                  "reason": "pin_not_found"})
            return {
                "success": False,
                "status_code": None,
                "error": {"message": f"Pinned key '{key_ref}' not in pool",
                          "code": "pin_not_found"},
                "key_ref": key_ref,
                "pin_failure": True,
            }

        key_idx = entry.get("index")
        raw_key = get_api_key_by_index(key_idx)
        if not raw_key:
            self.key_pool.report_result(key_ref, status_code=401)
            return {
                "success": False,
                "status_code": 401,
                "error": {"message": "Pinned key has no material",
                          "code": "pin_no_material"},
                "key_ref": key_ref,
                "pin_failure": True,
            }

        def _attempt():
            r = execute(raw_key)
            r["key_ref"] = key_ref
            r["key_index"] = key_idx
            return r

        result = _attempt()
        sc = result.get("status_code")
        if result.get("success") and sc == 200:
            self.key_pool.report_result(key_ref, status_code=200)
            return result

        # one same-key retry for transient errors only
        if sc is None or sc == 0 or sc >= 500:
            _dbg({"event": "pinned_retry", "key_ref": key_ref, "status_code": sc})
            result = _attempt()
            sc = result.get("status_code")
            if result.get("success") and sc == 200:
                self.key_pool.report_result(key_ref, status_code=200)
                return result

        if sc is not None:
            self.key_pool.report_result(key_ref, status_code=sc,
                                        error_data=result.get("error"))
        _dbg({"event": "pinned_failure", "key_ref": key_ref,
              "status_code": sc, "error": str(result.get("error"))[:200]})
        result["pin_failure"] = True
        return result

    def run(
        self,
        execute: Callable[[str], Dict[str, Any]],
        *,
        tool_bound: bool = False,
        pinned_key_ref: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Execute `execute(api_key)` with pool management.

        Args:
            execute: callable taking the raw API key, returning the normalized
                result dict (success/status_code/output/raw/error).
            tool_bound: True when this call carries a function_result for a
                prior function_call. Disables rotation/context-reset on error.
            pinned_key_ref: When set (active tool chain), use exactly this key
                and never rotate. See module docstring.
            timeout: passed through for logging only.
        """
        if pinned_key_ref:
            return self._run_pinned(execute, pinned_key_ref)

        discovered = self.key_pool.discover_keys()
        total_keys = len(discovered) if discovered else 1
        max_attempts = max(total_keys * 2, 4)

        excluded_keys: set = set()
        same_key_retried: set = set()
        force_key = None
        attempts = 0
        last_result: Optional[Dict[str, Any]] = None

        while attempts < max_attempts:
            attempts += 1

            try:
                if force_key is not None:
                    prefer = force_key
                else:
                    prefer = None
                    status = self.key_pool.get_status()
                    active = [k for k, v in status.items()
                              if v.get("state") == "ACTIVE" and k not in excluded_keys]
                    if active:
                        active.sort(key=lambda r: status[r].get("last_used_at", 0))
                        prefer = active[0]
                key_ref, key_idx = self.key_pool.acquire_key(
                    prefer_key=prefer, exclude_keys=excluded_keys)
                force_key = None
            except AllKeysExhaustedError:
                if last_result is not None:
                    return last_result
                return {
                    "success": False,
                    "status_code": None,
                    "error": {"message": "All API keys exhausted", "code": "all_keys_exhausted"},
                }

            raw_key = get_api_key_by_index(key_idx)
            if not raw_key:
                self.key_pool.report_result(key_ref, status_code=401)
                excluded_keys.add(key_ref)
                continue

            result = execute(raw_key)
            result["key_ref"] = key_ref
            result["key_index"] = key_idx
            last_result = result
            status_code = result.get("status_code")

            # 1. Success
            if result.get("success") and status_code == 200:
                self.key_pool.report_result(key_ref, status_code=200)
                return result

            # 2. Tool-bound turn: never rotate / reset context.
            #    Return the explicit error; caller must not claim success.
            if tool_bound:
                _dbg({
                    "event": "tool_bound_error",
                    "key_ref": key_ref,
                    "status_code": status_code,
                    "error": str(result.get("error"))[:200],
                    "attempt": attempts,
                })
                # Record the outcome (state machine only) without rotating.
                if status_code is not None:
                    self.key_pool.report_result(key_ref, status_code=status_code,
                                                error_data=result.get("error"))
                result["tool_bound_failure"] = True
                return result

            # 3. Key-change family (429 / 401 / 403)
            if status_code in _ROTATE_STATUS:
                _dbg({"event": "rotate", "reason": f"HTTP_{status_code}",
                      "key_ref": key_ref, "attempt": attempts})
                self.key_pool.report_result(key_ref, status_code=status_code,
                                            error_data=result.get("error"))
                excluded_keys.add(key_ref)
                continue

            # 4. Transient (5xx / network): one same-key retry, then rotate
            if status_code is None or status_code == 0 or status_code >= 500:
                if key_ref not in same_key_retried:
                    same_key_retried.add(key_ref)
                    force_key = key_ref  # retry the SAME key, not the next one
                    attempts -= 1  # don't burn the budget on the same-key retry
                    _dbg({"event": "same_key_retry", "key_ref": key_ref,
                          "status_code": status_code})
                    continue
                _dbg({"event": "rotate", "reason": "TRANSIENT",
                      "key_ref": key_ref, "attempt": attempts})
                self.key_pool.report_result(key_ref, status_code=status_code)
                excluded_keys.add(key_ref)
                continue

            # 5. Any other client error (400 etc.): no penalty, no retry
            self.key_pool.report_result(key_ref, status_code=status_code)
            return result

        return last_result or {
            "success": False,
            "status_code": None,
            "error": {"message": "Max attempts reached", "code": "max_attempts"},
        }
