import os
import re
import json
import time
import fcntl
import tempfile
from typing import Dict, Any, Optional, List, Tuple

DEFAULT_KEY_STORAGE_DIR = os.path.expanduser("~/.agy-router")
DEFAULT_KEY_STATE_PATH = os.path.join(DEFAULT_KEY_STORAGE_DIR, "key_states.json")

DEFAULT_COOLDOWN_SECONDS = 60
CONSECUTIVE_FAILURE_THRESHOLD = 3

class AllKeysExhaustedError(Exception):
    """Raised when no API keys are available in ACTIVE state."""
    pass

class KeyPoolCorruptedError(Exception):
    """Raised when the key state file is corrupted and cannot be safely parsed."""
    pass

def get_api_key_by_index(index: int = 1) -> str:
    """
    Retrieve API key for given 1-based index with priority:
    1. AGY_KEY_{index}
    2. GEMINI_API_KEY_{index} (or GEMINI_API_KEY for index 1)
    Also reads from ~/.bashrc if not present in os.environ.
    Never logs or exposes the key value.
    """
    primary_name = f"AGY_KEY_{index}"
    fallback_name = f"GEMINI_API_KEY_{index}" if index > 1 else "GEMINI_API_KEY"

    # 1. Check environment variables
    if primary_name in os.environ and os.environ[primary_name].strip():
        return os.environ[primary_name].strip()
    if fallback_name in os.environ and os.environ[fallback_name].strip():
        return os.environ[fallback_name].strip()

    # 2. Check ~/.bashrc directly (for daemon/subprocess sessions)
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
    """
    Thread-safe & process-safe key reference pool manager.
    Coordinates key states (ACTIVE, COOLDOWN, INACTIVE) and least-recently-used selection.
    Stores only symbolic references (e.g., 'key1', 'key2'), never raw secrets.
    """
    def __init__(self, storage_path: str = DEFAULT_KEY_STATE_PATH, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        self.storage_path = os.path.abspath(storage_path)
        self.storage_dir = os.path.dirname(self.storage_path)
        self.lock_path = self.storage_path + ".lock"
        self.cooldown_seconds = cooldown_seconds
        self._lock_depth = 0
        self._lock_fd = None
        self._ensure_storage_dir()

    def _ensure_storage_dir(self):
        if self.storage_dir and not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.storage_dir, 0o700)
            except OSError:
                pass

    def _with_lock(self, func):
        """Re-entrant file lock wrapper using POSIX advisory lock."""
        self._ensure_storage_dir()
        if self._lock_depth > 0:
            return func()

        self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            self._lock_depth += 1
            return func()
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0 and self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(self._lock_fd)
                self._lock_fd = None

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
        with tempfile.NamedTemporaryFile("w", dir=self.storage_dir, delete=False, encoding="utf-8") as tf:
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
        """
        Discovers all available 1-based key indices from environment and ~/.bashrc.
        Supports non-contiguous indices (e.g. 1, 3, 5).
        """
        indices = set()

        # 1. Inspect environment variables
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

        # 2. Inspect ~/.bashrc directly only if no keys found in environment
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
                        for pat in [re.compile(r"^export\s+AGY_KEY_(\d+)="), re.compile(r"^export\s+GEMINI_API_KEY_(\d+)=")]:
                            m = pat.match(line)
                            if m:
                                val = line.split("=", 1)[1].strip("\"' ")
                                if val:
                                    indices.add(int(m.group(1)))

        # Ensure that discovered indices actually yield a non-empty key
        valid_indices = []
        for idx in sorted(indices):
            if get_api_key_by_index(idx):
                valid_indices.append(idx)

        return valid_indices

    def _sync_discovered_keys_locked(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures all discovered keys exist in state dict."""
        discovered = self.discover_keys()
        keys_dict = data.setdefault("keys", {})
        for idx in discovered:
            ref = f"key{idx}"
            if ref not in keys_dict:
                keys_dict[ref] = {
                    "index": idx,
                    "state": "ACTIVE",
                    "fail_count": 0,
                    "last_status_code": None,
                    "cooldown_until": None,
                    "last_used_at": 0,
                    "last_success_at": 0,
                }
            else:
                # Ensure index is saved
                keys_dict[ref]["index"] = idx
        return data

    def acquire_key(self, prefer_key: Optional[str] = None) -> Tuple[str, int]:
        """
        Acquires an available ACTIVE key reference and its 1-based index under lock.
        Checks and recovers expired COOLDOWN states.
        Selects based on:
        1. prefer_key if specified and currently eligible
        2. Least-recently-used (lowest last_used_at) eligible key
        Returns (key_ref, key_index).
        Raises AllKeysExhaustedError if no keys are eligible.
        """
        def _tx():
            now = time.time()
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            keys_dict = data["keys"]

            # Evaluate cooldown expirations
            for ref, entry in keys_dict.items():
                if entry.get("state") == "COOLDOWN":
                    cooldown_until = entry.get("cooldown_until") or 0
                    if now >= cooldown_until:
                        entry["state"] = "ACTIVE"
                        entry["cooldown_until"] = None
                        entry["fail_count"] = 0

            # Eligible candidates: state == 'ACTIVE'
            eligible = [ref for ref, e in keys_dict.items() if e.get("state") == "ACTIVE"]

            if not eligible:
                self._save_raw(data)
                raise AllKeysExhaustedError("All API keys in the pool are exhausted (INACTIVE or in COOLDOWN).")

            chosen_ref = None
            if prefer_key and prefer_key in eligible:
                chosen_ref = prefer_key
            else:
                # Least-recently-used selection
                eligible.sort(key=lambda r: keys_dict[r].get("last_used_at", 0))
                chosen_ref = eligible[0]

            # Mark last_used_at atomically
            keys_dict[chosen_ref]["last_used_at"] = now
            self._save_raw(data)

            return chosen_ref, keys_dict[chosen_ref]["index"]

        return self._with_lock(_tx)

    def report_result(
        self,
        key_ref: str,
        status_code: Optional[int] = None,
        error_data: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Updates key state based on HTTP status / error outcome.
        - 200: fail_count=0, state=ACTIVE, last_success_at=now
        - 429: state=COOLDOWN, cooldown_until=now+cooldown_seconds
        - 401: state=INACTIVE
        - 403: increment fail_count; if >= threshold -> COOLDOWN (does NOT mark INACTIVE blindly)
        - 500/502/503/504: increment fail_count; if >= threshold -> COOLDOWN
        - timeout / network error: increment fail_count; if >= threshold -> COOLDOWN
        - 400 / other 4xx client errors: no rotation/penalty
        """
        def _tx():
            now = time.time()
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            keys_dict = data["keys"]

            if key_ref not in keys_dict:
                # If key not in dict, create a stub
                m = re.match(r"^key(\d+)$", key_ref)
                idx = int(m.group(1)) if m else 1
                keys_dict[key_ref] = {
                    "index": idx,
                    "state": "ACTIVE",
                    "fail_count": 0,
                    "last_status_code": None,
                    "cooldown_until": None,
                    "last_used_at": now,
                    "last_success_at": 0,
                }

            entry = keys_dict[key_ref]
            entry["last_status_code"] = status_code

            # 1. Success
            if status_code == 200:
                entry["state"] = "ACTIVE"
                entry["fail_count"] = 0
                entry["cooldown_until"] = None
                entry["last_success_at"] = now

            # 2. Rate limit (429)
            elif status_code == 429:
                entry["state"] = "COOLDOWN"
                entry["fail_count"] += 1
                entry["cooldown_until"] = now + self.cooldown_seconds

            # 3. Invalid credentials (401)
            elif status_code == 401:
                entry["state"] = "INACTIVE"
                entry["fail_count"] += 1
                entry["cooldown_until"] = None

            # 4. Forbidden (403) - do NOT blindly mark INACTIVE
            elif status_code == 403:
                entry["fail_count"] += 1
                if entry["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    entry["state"] = "COOLDOWN"
                    entry["cooldown_until"] = now + self.cooldown_seconds

            # 5. Server errors (5xx)
            elif status_code in (500, 502, 503, 504):
                entry["fail_count"] += 1
                if entry["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    entry["state"] = "COOLDOWN"
                    entry["cooldown_until"] = now + self.cooldown_seconds

            # 6. Timeout / Network error (status_code is None or 0)
            elif status_code is None or status_code == 0:
                entry["fail_count"] += 1
                if entry["fail_count"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    entry["state"] = "COOLDOWN"
                    entry["cooldown_until"] = now + self.cooldown_seconds

            # 7. 400 and other client errors: no rotation penalty
            else:
                pass

            self._save_raw(data)
            return entry

        return self._with_lock(_tx)

    def rotate(self, current_key_ref: Optional[str] = None) -> Tuple[str, int]:
        """
        Forces acquisition of a key other than current_key_ref if available.
        """
        def _tx():
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            keys_dict = data["keys"]

            now = time.time()
            for ref, entry in keys_dict.items():
                if entry.get("state") == "COOLDOWN":
                    cooldown_until = entry.get("cooldown_until") or 0
                    if now >= cooldown_until:
                        entry["state"] = "ACTIVE"
                        entry["cooldown_until"] = None
                        entry["fail_count"] = 0

            eligible = [ref for ref, e in keys_dict.items() if e.get("state") == "ACTIVE"]
            if not eligible:
                self._save_raw(data)
                raise AllKeysExhaustedError("All API keys in the pool are exhausted.")

            # Filter out current_key_ref if other eligible keys exist
            others = [r for r in eligible if r != current_key_ref]
            candidates = others if others else eligible

            candidates.sort(key=lambda r: keys_dict[r].get("last_used_at", 0))
            chosen = candidates[0]
            keys_dict[chosen]["last_used_at"] = now
            self._save_raw(data)
            return chosen, keys_dict[chosen]["index"]

        return self._with_lock(_tx)

    def reset_key(self, key_ref: str) -> bool:
        """Resets a key's state back to ACTIVE."""
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
        """Returns status map of all discovered keys."""
        def _tx():
            data = self._load_raw()
            self._sync_discovered_keys_locked(data)
            self._save_raw(data)
            return data.get("keys", {})

        return self._with_lock(_tx)
