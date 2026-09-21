import os
import json
import tempfile
import fcntl
import time
from typing import Dict, Any, Optional, List
from file_lock import FileAndThreadLock


DEFAULT_SESSION_DIR = os.path.expanduser("~/.agy-router")
DEFAULT_SESSION_PATH = os.path.join(DEFAULT_SESSION_DIR, "sessions.json")

class SessionCorruptedError(Exception):
    """Raised when the sessions file is corrupted and cannot be safely parsed."""
    pass

class SessionStateManager:
    """
    Manages isolated, multi-session runtime state for projects.
    Decoupled from static Project metadata (projects.json) and Key pool (key_states.json).
    Ensures safe multi-session isolation, process-level atomic flock, and state transitions.
    """
    VALID_STATES = {"IDLE", "ACTIVE", "INVALIDATED"}

    def __init__(self, storage_path: str = DEFAULT_SESSION_PATH):
        self.storage_path = os.path.abspath(storage_path)
        self.storage_dir = os.path.dirname(self.storage_path)
        self.lock_path = self.storage_path + ".lock"
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
        """Re-entrant file and thread lock wrapper."""
        self._ensure_storage_dir()
        return self._lock.run(func)

    def load(self) -> Dict[str, Any]:
        def _read():
            if not os.path.exists(self.storage_path):
                return {"sessions": {}}

            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        return {"sessions": {}}
                    data = json.loads(content)
                    if not isinstance(data, dict) or "sessions" not in data:
                        raise ValueError("Sessions JSON structure missing top-level 'sessions' dict.")
                    return data
            except Exception as e:
                raise SessionCorruptedError(
                    f"Sessions file at '{self.storage_path}' is corrupted or unreadable: {e}. "
                    "Refusing to overwrite existing state."
                ) from e

        return self._with_lock(_read)

    def save(self, data: Dict[str, Any]):
        def _write():
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

        self._with_lock(_write)

    def create_session(
        self,
        project_id: str,
        session_id: Optional[str] = None,
        bound_key: Optional[str] = None,
        environment_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates and registers a new session scoped to a project.
        """
        if not project_id:
            raise ValueError("project_id must be provided to create a session.")

        sid = session_id or f"sess_{project_id}_{int(time.time() * 1000)}"
        now = time.time()

        def _tx():
            data = self.load()
            if sid in data["sessions"]:
                # If already exists, return existing or update if provided
                return data["sessions"][sid]

            entry = {
                "session_id": sid,
                "project_id": project_id,
                "bound_key": bound_key or "key1",
                "tenant_id": tenant_id or "",
                "environment_id": environment_id or "",
                "last_interaction_id": "",
                "state": "IDLE",
                "created_at": now,
                "updated_at": now
            }
            data["sessions"][sid] = entry
            self.save(data)
            return entry

        return self._with_lock(_tx)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        data = self.load()
        return data["sessions"].get(session_id)

    def list_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        data = self.load()
        sessions = list(data["sessions"].values())
        if project_id:
            return [s for s in sessions if s.get("project_id") == project_id]
        return sessions

    def update_session(
        self,
        session_id: str,
        bound_key: Optional[str] = None,
        environment_id: Optional[str] = None,
        last_interaction_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        def _tx():
            data = self.load()
            if session_id not in data["sessions"]:
                raise KeyError(f"Session '{session_id}' not found in storage.")

            s = data["sessions"][session_id]
            if bound_key is not None:
                s["bound_key"] = bound_key
            if environment_id is not None:
                s["environment_id"] = environment_id
            if last_interaction_id is not None:
                s["last_interaction_id"] = last_interaction_id
            if tenant_id is not None:
                s["tenant_id"] = tenant_id
            if state is not None:
                if state not in self.VALID_STATES:
                    raise ValueError(f"Invalid session state: '{state}'. Must be one of {self.VALID_STATES}")
                s["state"] = state
            s["updated_at"] = time.time()

            data["sessions"][session_id] = s
            self.save(data)
            return s

        return self._with_lock(_tx)

    def invalidate_session(self, session_id: str) -> Dict[str, Any]:
        """Explicitly invalidates a session on tenant mismatch, 404, or environment loss."""
        return self.update_session(session_id=session_id, state="INVALIDATED")

    def delete_session(self, session_id: str) -> bool:
        def _tx():
            data = self.load()
            if session_id in data["sessions"]:
                del data["sessions"][session_id]
                self.save(data)
                return True
            return False

        return self._with_lock(_tx)
