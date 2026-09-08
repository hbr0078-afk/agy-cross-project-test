import os
import json
import tempfile
import subprocess
from typing import Dict, Any, Optional, List

DEFAULT_REGISTRY_PATH = os.path.expanduser("~/.agy-router/registry.json")

class RegistryCorruptedError(Exception):
    """Raised when the registry file is corrupted and cannot be safely parsed."""
    pass

class ProjectRegistry:
    def __init__(self, storage_path: str = DEFAULT_REGISTRY_PATH):
        self.storage_path = os.path.abspath(storage_path)
        self._ensure_storage_dir()

    def _ensure_storage_dir(self):
        dir_name = os.path.dirname(self.storage_path)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        """
        Loads and returns the registry data.
        Raises RegistryCorruptedError if the file exists but contains invalid JSON.
        """
        if not os.path.exists(self.storage_path):
            return {"projects": {}}

        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"projects": {}}
                data = json.loads(content)
                if not isinstance(data, dict) or "projects" not in data:
                    raise ValueError("Registry JSON structure missing top-level 'projects' dict.")
                return data
        except Exception as e:
            raise RegistryCorruptedError(
                f"Registry file at '{self.storage_path}' is corrupted or unreadable: {e}. "
                "Refusing to overwrite existing state."
            ) from e

    def save(self, data: Dict[str, Any]):
        """
        Atomically saves data to storage_path using temp file + fsync + os.replace.
        """
        self._ensure_storage_dir()
        dir_name = os.path.dirname(self.storage_path)
        
        # Write to temporary file in the same directory for atomic replace
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
            temp_path = tf.name

        os.replace(temp_path, self.storage_path)

    def register_project(self, project_path: str, project_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Registers a local directory as a project.
        Extracts git remote and branch if available.
        """
        abs_path = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(abs_path):
            raise FileNotFoundError(f"Project directory does not exist: {abs_path}")

        pid = project_id or os.path.basename(abs_path)
        
        # Detect git info if present
        repo = ""
        branch = "main"
        last_commit = ""
        git_dir = os.path.join(abs_path, ".git")
        if os.path.exists(git_dir):
            try:
                # get remote url
                r = subprocess.run(
                    ["git", "-C", abs_path, "remote", "get-url", "origin"],
                    capture_output=True, text=True, check=False
                )
                if r.returncode == 0:
                    repo = r.stdout.strip()
                # get branch
                b = subprocess.run(
                    ["git", "-C", abs_path, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, check=False
                )
                if b.returncode == 0 and b.stdout.strip():
                    branch = b.stdout.strip()
                # get last commit
                c = subprocess.run(
                    ["git", "-C", abs_path, "rev-parse", "HEAD"],
                    capture_output=True, text=True, check=False
                )
                if c.returncode == 0:
                    last_commit = c.stdout.strip()
            except Exception:
                pass

        data = self.load()
        existing = data["projects"].get(pid, {})
        
        project_entry = {
            "project_id": pid,
            "path": abs_path,
            "repo": repo or existing.get("repo", ""),
            "branch": branch or existing.get("branch", "main"),
            "active_key": existing.get("active_key", "key1"),
            "environment_id": existing.get("environment_id", ""),
            "last_interaction_id": existing.get("last_interaction_id", ""),
            "last_commit": last_commit or existing.get("last_commit", ""),
            "state": existing.get("state", "IDLE"),
            "roadmap": existing.get("roadmap", "ROADMAP.md")
        }

        data["projects"][pid] = project_entry
        self.save(data)
        return project_entry

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        data = self.load()
        return data["projects"].get(project_id)

    def find_project_by_path(self, current_dir: str) -> Optional[Dict[str, Any]]:
        abs_dir = os.path.abspath(current_dir)
        data = self.load()
        for p in data["projects"].values():
            p_path = p.get("path")
            if p_path and (abs_dir == p_path or abs_dir.startswith(p_path + os.sep)):
                return p
        return None

    def list_projects(self) -> List[Dict[str, Any]]:
        data = self.load()
        return list(data["projects"].values())

    def update_project_state(
        self,
        project_id: str,
        environment_id: Optional[str] = None,
        last_interaction_id: Optional[str] = None,
        last_commit: Optional[str] = None,
        active_key: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = self.load()
        if project_id not in data["projects"]:
            raise KeyError(f"Project '{project_id}' not found in registry.")

        p = data["projects"][project_id]
        if environment_id is not None:
            p["environment_id"] = environment_id
        if last_interaction_id is not None:
            p["last_interaction_id"] = last_interaction_id
        if last_commit is not None:
            p["last_commit"] = last_commit
        if active_key is not None:
            p["active_key"] = active_key
        if state is not None:
            p["state"] = state

        data["projects"][project_id] = p
        self.save(data)
        return p
