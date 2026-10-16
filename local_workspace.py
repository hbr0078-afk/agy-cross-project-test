"""
Phase 8-2: Shared LOCAL workspace as the source of truth.

The router — not the model — owns the local files. For every request we collect
the current local file state, hand the required content to the model, and apply
ONLY the validated changes atomically. Nothing here talks to the remote
Environment File API; this is deliberately separate from workspace_sync /
environment_files (legacy remote-environment path).
"""
import os
import re
import json
import time
import hashlib
import tempfile
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from file_lock import FileAndThreadLock

DEFAULT_MAX_FILE_SIZE_BYTES = 512 * 1024

# Basename tokens that mark a file as secret-bearing; never read into context,
# never written back. Matched against whitespace/punctuation-split tokens.
_SECRET_TOKENS = {"KEY", "KEYS", "TOKEN", "TOKENS", "SECRET", "SECRETS",
                  "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS"}
_SECRET_NAMES = {".env", ".netrc", "id_rsa", "id_ed25519", "credentials", ".git-credentials"}


class WorkspaceError(Exception):
    """Raised for unsafe paths, malformed model output, or validation failure."""
    pass


def _split_tokens(name: str) -> List[str]:
    return [p for p in re.split(r"[^A-Za-z0-9]+", name.upper()) if p]


def is_secret_path(rel_path: str) -> bool:
    """True if the path's basename looks like it holds credentials."""
    base = os.path.basename(rel_path)
    if base in _SECRET_NAMES or base.startswith(".env"):
        return True
    return any(tok in _SECRET_TOKENS for tok in _split_tokens(base))


def _extract_json_block(output_text: str) -> Dict[str, Any]:
    """Pull the JSON envelope out of a model response (fenced block or bare object)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output_text, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start, end = output_text.find("{"), output_text.rfind("}")
        if start == -1 or end <= start:
            raise WorkspaceError("model output contains no JSON object")
        candidate = output_text[start:end + 1]
    try:
        payload = json.loads(candidate)
    except (ValueError, TypeError) as e:
        raise WorkspaceError(f"model output is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise WorkspaceError("model output JSON must be an object")
    return payload


class LocalWorkspace:
    """Local file state + checkpoint, guarded by the repo-wide file/thread lock."""

    def __init__(self, root: str, state_dir: Optional[str] = None,
                 max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES):
        self.root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(self.root):
            raise WorkspaceError(f"Workspace root does not exist: {self.root}")
        self.state_dir = os.path.abspath(state_dir or os.path.join(self.root, ".agy-router"))
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.checkpoint_path = os.path.join(self.state_dir, "checkpoint.json")
        self.lock_path = os.path.join(self.state_dir, "workspace.lock")
        self.max_file_size_bytes = max_file_size_bytes
        self._lock = FileAndThreadLock(self.lock_path)

    # ---- path safety -----------------------------------------------------
    def safe_path(self, rel_path: str) -> Tuple[str, str]:
        """Validate rel_path and return (normalised_rel, absolute_path)."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise WorkspaceError("empty file path")
        if "\x00" in rel_path:
            raise WorkspaceError("file path contains NUL byte")
        if os.path.isabs(rel_path):
            raise WorkspaceError(f"absolute paths are not allowed: {rel_path}")
        norm = os.path.normpath(rel_path).replace(os.sep, "/")
        if norm == ".." or norm.startswith("../") or norm == ".":
            raise WorkspaceError(f"path traversal detected: {rel_path}")
        abs_path = os.path.abspath(os.path.join(self.root, norm))
        if abs_path != self.root and not abs_path.startswith(self.root + os.sep):
            raise WorkspaceError(f"path escapes workspace: {rel_path}")
        return norm, abs_path

    # ---- read side -------------------------------------------------------
    def collect_context(self, required_files: Optional[List[str]] = None,
                        task_context: Optional[str] = None,
                        prev_task_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Gather required file content + task context + previous task state."""
        files: Dict[str, str] = {}
        for rel in (required_files or []):
            try:
                norm, abs_path = self.safe_path(rel)
            except WorkspaceError:
                continue
            if is_secret_path(norm) or not os.path.isfile(abs_path):
                continue
            try:
                if os.path.getsize(abs_path) > self.max_file_size_bytes:
                    continue
                with open(abs_path, "r", encoding="utf-8") as f:
                    files[norm] = f.read()
            except OSError:
                continue
        return {
            "task_context": task_context or "",
            "files": dict(sorted(files.items())),
            "prev_task_state": prev_task_state if prev_task_state is not None
            else self.load_checkpoint().get("last_good_state", {}),
        }

    # ---- write side ------------------------------------------------------
    def parse_changes(self, output_text: str) -> Dict[str, str]:
        """Turn a model response into {rel_path: content}, or raise WorkspaceError."""
        payload = _extract_json_block(output_text)
        files = payload.get("files")
        if files is None:
            raise WorkspaceError("model output missing 'files'")
        if isinstance(files, dict):
            items = list(files.items())
        elif isinstance(files, list):
            items = [(it.get("path"), it.get("content"))
                     for it in files if isinstance(it, dict)]
        else:
            raise WorkspaceError("model output 'files' must be an object or list")

        changes: Dict[str, str] = {}
        for rel, content in items:
            norm, _ = self.safe_path(rel)
            if is_secret_path(norm):
                raise WorkspaceError(f"refusing to write secret-like path: {norm}")
            if not isinstance(content, str):
                raise WorkspaceError(f"invalid content for {norm}")
            if norm.endswith(".py"):
                try:
                    compile(content, norm, "exec")
                except SyntaxError as e:
                    raise WorkspaceError(f"syntax error in {norm}: {e}") from e
            changes[norm] = content
        if not changes:
            raise WorkspaceError("model output contained no file changes")
        return changes

    def apply_changes(self, changes: Dict[str, str]) -> List[str]:
        """Validate all targets, stage temp files, then atomically replace. All-or-nothing."""
        def _tx() -> List[str]:
            staged: List[Tuple[str, str, str]] = []  # (temp, target, norm)
            try:
                for rel, content in changes.items():
                    norm, abs_path = self.safe_path(rel)
                    if is_secret_path(norm):
                        raise WorkspaceError(f"refusing to write secret-like path: {norm}")
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        "w", dir=os.path.dirname(abs_path), delete=False, encoding="utf-8"
                    ) as tf:
                        tf.write(content)
                        tf.flush()
                        os.fsync(tf.fileno())
                        tmp = tf.name
                    staged.append((tmp, abs_path, norm))
            except Exception:
                for tmp, _, _ in staged:
                    _quiet_remove(tmp)
                raise
            changed = []
            for tmp, abs_path, norm in staged:
                os.replace(tmp, abs_path)
                changed.append(norm)
            return changed

        return self._lock.run(_tx)

    # ---- checkpoint ------------------------------------------------------
    def load_checkpoint(self) -> Dict[str, Any]:
        def _read() -> Dict[str, Any]:
            if not os.path.exists(self.checkpoint_path):
                return {}
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (ValueError, OSError):
                return {}
        return self._lock.run(_read)

    def save_checkpoint(self, goal: Optional[str] = None, next_todo: Optional[str] = None,
                        changed_files: Optional[List[str]] = None,
                        test_results: Optional[Dict[str, Any]] = None,
                        last_good_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        def _write() -> Dict[str, Any]:
            data = self.load_checkpoint()
            if goal is not None:
                data["goal"] = goal
            if next_todo is not None:
                data["next_todo"] = next_todo
            if changed_files is not None:
                data["changed_files"] = changed_files
            if test_results is not None:
                data["test_results"] = test_results
            if last_good_state is not None:
                data["last_good_state"] = last_good_state
            data["updated_at"] = time.time()
            with tempfile.NamedTemporaryFile(
                "w", dir=self.state_dir, delete=False, encoding="utf-8"
            ) as tf:
                json.dump(data, tf, indent=2, ensure_ascii=False)
                tf.flush()
                os.fsync(tf.fileno())
                tmp = tf.name
            os.replace(tmp, self.checkpoint_path)
            return data
        return self._lock.run(_write)

    def run_tests(self, command: List[str], timeout: int = 300) -> Dict[str, Any]:
        """Run an explicit test command against the workspace (no shell, no env leak)."""
        if not command:
            return {"ran": False, "reason": "no command provided"}
        try:
            proc = subprocess.run(
                list(command), cwd=self.root, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return {"ran": True, "returncode": None, "error": str(e)}
        return {"ran": True, "returncode": proc.returncode,
                "output": (proc.stdout or "")[-4000:]}


def _quiet_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def file_sha256(path: str) -> str:
    """Stable content hash used for last-good-state bookkeeping."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
