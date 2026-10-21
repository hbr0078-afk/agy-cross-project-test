"""Ported key pool for the Antigravity adapter.

Derived from agy-router/keys.py, trimmed to what the adapter actually needs:
  - discover / acquire / report_result / rotate / reset_key / get_status
  - state machine preserved verbatim (429 -> COOLDOWN, 401 -> INACTIVE,
    403/5xx/network -> threshold COOLDOWN, other 4xx -> no penalty)
Removed:
  - project/registry binding branch (adapter has no ProjectRegistry)
Dependencies:
  - file_lock.FileAndThreadLock (port kept local)
  - local masked logger (replaces observability's 4 key log functions)

Secrets discipline: only symbolic refs ("key1"..."keyN") are persisted.
Raw key material lives in env / ~/.bashrc and is read on demand only.
"""

import os
import re
import json
import time
import fcntl  # noqa: F401  (kept for parity with upstream import block)
import tempfile
import sys
from typing import Dict, Any, Optional, List, Tuple

from file_lock import FileAndThreadLock


DEFAULT_KEY_STORAGE_DIR = os.path.expanduser("~/.agy-router")
DEFAULT_KEY_STATE_PATH = os.path.join(DEFAULT_KEY_STORAGE_DIR, "key_states.json")

DEFAULT_COOLDOWN_SECONDS = 60
CONSECUTIVE_FAILURE_THRESHOLD = 3

# Shared key-state file with agy-router is intentional: the adapter and chi
# observe the same pool so a cooldown learned in one place applies in both.
# Override with AGY_ADAPTER_KEY_STATE if isolation is ever required.
_KEY_STATE_PATH = os.environ.get("AGY_ADAPTER_KEY_STATE", DEFAULT_KEY_STATE_PATH)


class AllKeysExhaustedError(Exception):
    """Raised when no API keys are available in ACTIVE state."""


class KeyPoolCorruptedError(Exception):
    """Raised when the key state file is corrupted and cannot be safely parsed."""


# --- local masked logger (replaces observability.log_key_* 4 functions) ---

def _mask_ref(key_ref: str) -> str:
    """Symbolic refs are already safe; never emit raw material."""
    return key_ref or "key?"


def _klog(event: str, key_ref: str, **fields: Any) -> None:
    """Structured, secret-free key-pool log line. Controlled by AGY_ADAPTER_DEBUG."""
    if not os.environ.get("AGY_ADAPTER_DEBUG"):
        return
    try:
        rec = {"event": event, "key_ref": _mask_ref(key_ref), "ts": time.time()}
        for k, v in fields.items():
            if k in ("api_key", "key", "secret", "token"):
                continue  # never log raw material even if a caller passes it
            rec[k] = v
        with open("/tmp/agy_adapter_keypool.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_api_key_by_index(index: int = 1) -> str:
    """Retrieve API key for 1-based index.

    Priority: AGY_KEY_{i} > GEMINI_API_KEY_{i} (GEMINI_API_KEY for i==1),
    then ~/.bashrc. Never logs the value.
    """
    primary_name = f"AGY_KEY_{index}"
    fallback_name = f"GEMINI_API_KEY_{index}" if index > 1 else "GEMINI_API_KEY"

    if primary_name in os.environ and os.environ[primary_name].strip():
        return os.environ[primary_name].strip()
    if fallback_name in os.environ and os.environ[fallback_name].strip():
        return os.environ[fallback_name].strip()

    bashrc_path = os.path.expanduser("~/.bashrc")
    primary_val = ""
    fallback_val = ""
    if os.path.exists(bashrc_path):
        with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"export {primary_name}="):
                    val = line.split("=", 1)[1].strip("\"' ")
                    if val:
                        primary_val = val
                elif line.startswith(f"export {fallback_name}="):
                    val = line.split("=", 1)[1].strip("\"' ")
                    if val:
                        fallback_val = val

    if primary_val:
        return primary_val
    if fallback_val:
        return fallback_val
    return ""


class KeyPoolManager:
    """Thread-safe & process-safe key reference pool manager.

    States: ACTIVE, COOLDOWN, INACTIVE. LRU selection. Symbolic refs only.
    """

    def __init__(self, storage_path: str = _KEY_STATE_PATH,
                 cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        self.storage_path = os.path.abspath(storage_path)
        self.storage_dir = os.path.dirname(self.storage_path)
        self.lock_path = self.storage_path + ".lock"
        self.cooldown_seconds = cooldown_seconds
        self._lock = FileAndThreadLock(self.lock_path)
        self._ensure_storage_dir()

    def _ensure_storage_dir(self):
        if self.storage_dir and not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.storage_dir, 0o700)
            except OSError:
                pass

    def _with_lock(self, func):
        self._ensure_storage_dir()
        return self._lock.run(func)

    def _load_raw(self) -> Dict[str, Any]:
        if not os.path.exists(self.storage_path):
            return {"keys": {}}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"keys": {}}
                data = json.loads(content)
                if not isinstance(data, dict) or "keys" not in data:
                    raise ValueError("Key states JSON missing top-level 'keys' dict.")
                return data
        except Exception as e:
            raise KeyPoolCorruptedError(
                f"Key state file at '{self.storage_path}' is corrupted: {e}"
            ) from e

    def _save_raw(self, data: Dict[str, Any]):
        self._ensure_storage_dir()
        with tempfile.NamedTemporaryFile("w", dir=self.storage_dir, delete=False,
                                         encoding="utf-8") as tf:
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
            temp_path = tf.name
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, self.storage_path)

    def discover_keys(self) -> List[int]:
        """Discover available 1-based key indices from env and ~/.bashrc."""
        indices = set()
        patterns = [
            re.compile(r"^AGY_KEY_(\d+)$"),
            re.compile(r"^GEMINI_API_KEY_(\d+)$"),
        ]
        has_env_key = False
        if "GEMINI_API_KEY" in os.environ and os.environ["GEMINI_API_KEY"].strip():
            indices.add(1)
            has_env_key = True

        for k, v in os.environ.items():
            if not v or not v.strip():
                continue
            for pat in patterns:
                m = pat.match(k)
                if m:
                    indices.add(int(m.group(1)))
                    has_env_key = True

        if not has_env_key:
            bashrc_path = os.path.expanduser("~/.bashrc")
            if os.path.exists(bashrc_path):
                with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("export GEMINI_API_KEY="):
                            val = line.split("=", 1)[1].strip("\"' ")
                            if val:
                                indices.add(1)
                        for pat in [re.compile(r"^export\s+AGY_KEY_(\d+)="),
                                    re.compile(r"^export\s+GEMINI_API_KEY_(\d+)=")]:
                            m = pat.match(line)
                            if m:
                                val = line.split("=", 1)[1].strip("\"' ")
                                if val:
                                    indices.add(int(m.group(1)))

        valid_indices = []
        for idx in sorted(indices):
            if get_api_key_by_index(idx):
                valid_indices.append(idx)
        return valid_indices

    def _sync_discovered_keys_locked(self, data: Dict[str, Any]) -> Dict[str, Any]:
        discovered = self.discover_keys()
        keys_dict = data.setdefault("keys", {})
        for idx in discovered:
            ref = f"key{idx}"
            tenant_id = (os.environ.get(f"AGY_TENANT_{idx}")
                         or os.environ.get(f"GEMINI_TENANT_{idx}") or "")
            if ref not in keys_dict:
                keys_dict[ref] = {
                    "index": idx,
                    "state": "ACTIVE",
                    "fail_count": 0,
                    "last_status_code": None,
                    "cooldown_until": None,
                    "last_used_at": 0,
                    "last_success_at": 0,
                    "tenant_id": tenant_id,
                }
            else:
                keys_dict[ref]["index"] = idx
                if tenant_id and keys_dict[ref].get("tenant_id") != tenant_id:
                    keys_dict[ref]["tenant_id"] = tenant_id
        return data

    def get_key_tenant(self, key_ref: str) -> str:
        def _read():
            data = self._load_raw()
            entry = data.get("keys", {}).get(key_ref)
            return entry.get("tenant_id", "") if entry else ""
        return self._with_lock(_read)

    def acquire_key(
        self,
        prefer_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        exclude_keys: Optional[Any] = None,
    ) -> Tuple[str, int]:
        """Acquire an eligible ACTIVE key ref + 1-based index under lock.

        Priority: tenant match (if given) > prefer_key > LRU.
        Raises AllKeysExhaustedError if none eligible.
        """
        def _tx():
            now = time.time()
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            keys_dict = data["keys"]

            for ref, entry in keys_dict.items():
                if entry.get("state") == "COOLDOWN":
                    cooldown_until = entry.get("cooldown_until") or 0
                    if now >= cooldown_until:
                        entry["state"] = "ACTIVE"
                        entry["cooldown_until"] = None
                        entry["fail_count"] = 0

            excluded_set = set(exclude_keys) if exclude_keys else set()
            eligible = [ref for ref, e in keys_dict.items()
                        if e.get("state") == "ACTIVE" and ref not in excluded_set]

            if not eligible:
                self._save_raw(data)
                raise AllKeysExhaustedError(
                    "All API keys in the pool are exhausted (INACTIVE or COOLDOWN).")

            chosen_ref = None
            same_tenant_eligible = []
            if tenant_id:
                same_tenant_eligible = [
                    r for r in eligible if keys_dict[r].get("tenant_id") == tenant_id]

            candidates = same_tenant_eligible if same_tenant_eligible else eligible

            if prefer_key and prefer_key in candidates:
                chosen_ref = prefer_key
            elif prefer_key and not same_tenant_eligible and prefer_key in eligible:
                chosen_ref = prefer_key

            if not chosen_ref:
                candidates.sort(key=lambda r: keys_dict[r].get("last_used_at", 0))
                chosen_ref = candidates[0]

            keys_dict[chosen_ref]["last_used_at"] = now
            self._save_raw(data)
            _klog("key_selected", chosen_ref,
                  tenant=keys_dict[chosen_ref].get("tenant_id") or None)
            return chosen_ref, keys_dict[chosen_ref]["index"]

        return self._with_lock(_tx)

    def report_result(
        self,
        key_ref: str,
        status_code: Optional[int] = None,
        error_data: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Update key state from an HTTP outcome (state machine preserved)."""
        def _tx():
            now = time.time()
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            keys_dict = data["keys"]

            if key_ref not in keys_dict:
                m = re.match(r"^key(\d+)$", key_ref)
                idx = int(m.group(1)) if m else 1
                keys_dict[key_ref] = {
                    "index": idx, "state": "ACTIVE", "fail_count": 0,
                    "last_status_code": None, "cooldown_until": None,
                    "last_used_at": now, "last_success_at": 0, "tenant_id": "",
                }

            entry = keys_dict[key_ref]
            entry["last_status_code"] = status_code

            if status_code == 200:
                entry["state"] = "ACTIVE"
                entry["fail_count"] = 0
                entry["cooldown_until"] = None
                entry["last_success_at"] = now
            elif status_code == 429:
                entry["state"] = "COOLDOWN"
                entry["fail_count"] += 1
                entry["cooldown_until"] = now + self.cooldown_seconds
                _klog("key_cooldown", key_ref, seconds=self.cooldown_seconds,
                      reason="RATE_LIMIT_429", tenant=entry.get("tenant_id"))
            elif status_code == 401:
                entry["state"] = "INACTIVE"
                entry["fail_count"] += 1
                entry["cooldown_until"] = None
                _klog("key_inactive", key_ref,
                      reason="INVALID_CREDENTIALS_401", tenant=entry.get("tenant_id"))
            elif status_code == 403:
                entry["fail_count"] += 1
                if entry["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    entry["state"] = "COOLDOWN"
                    entry["cooldown_until"] = now + self.cooldown_seconds
                    _klog("key_cooldown", key_ref, seconds=self.cooldown_seconds,
                          reason="FORBIDDEN_403_THRESHOLD", tenant=entry.get("tenant_id"))
            elif status_code in (500, 502, 503, 504):
                entry["fail_count"] += 1
                if entry["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    entry["state"] = "COOLDOWN"
                    entry["cooldown_until"] = now + self.cooldown_seconds
                    _klog("key_cooldown", key_ref, seconds=self.cooldown_seconds,
                          reason=f"SERVER_ERROR_{status_code}_THRESHOLD",
                          tenant=entry.get("tenant_id"))
            elif status_code is None or status_code == 0:
                entry["fail_count"] += 1
                if entry["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    entry["state"] = "COOLDOWN"
                    entry["cooldown_until"] = now + self.cooldown_seconds
                    _klog("key_cooldown", key_ref, seconds=self.cooldown_seconds,
                          reason="NETWORK_ERROR_THRESHOLD", tenant=entry.get("tenant_id"))
            else:
                pass  # 400 / other client errors: no penalty

            self._save_raw(data)
            return entry

        return self._with_lock(_tx)

    def reset_key(self, key_ref: str) -> bool:
        def _tx():
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            if key_ref in data["keys"]:
                data["keys"][key_ref]["state"] = "ACTIVE"
                data["keys"][key_ref]["fail_count"] = 0
                data["keys"][key_ref]["cooldown_until"] = None
                self._save_raw(data)
                return True
            return False
        return self._with_lock(_tx)

    def get_status(self) -> Dict[str, Any]:
        """Read-only status map (never persists discovery here)."""
        def _tx():
            data = self._load_raw()
            discovered = self.discover_keys()
            keys_dict = data.get("keys", {})
            for idx in discovered:
                ref = f"key{idx}"
                tenant_id = (os.environ.get(f"AGY_TENANT_{idx}")
                             or os.environ.get(f"GEMINI_TENANT_{idx}") or "")
                if ref not in keys_dict:
                    keys_dict[ref] = {
                        "index": idx, "state": "ACTIVE", "fail_count": 0,
                        "last_status_code": None, "cooldown_until": None,
                        "last_used_at": 0, "last_success_at": 0,
                        "tenant_id": tenant_id,
                    }
                elif tenant_id and keys_dict[ref].get("tenant_id") != tenant_id:
                    keys_dict[ref]["tenant_id"] = tenant_id
            return keys_dict
        return self._with_lock(_tx)
