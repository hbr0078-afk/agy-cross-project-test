import os
import io
import re
import hashlib
import tarfile
import requests
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from keys import get_api_key_by_index

def mask_credentials(text: str) -> str:
    """Mask tokens, passwords, and API keys from logs/outputs."""
    if not text:
        return ""
    masked = re.sub(r'(https?://[^:\s]+:)([^@\s]+)(@)', r'\1***\3', text)
    masked = re.sub(r'(gh[pousr]_[A-Za-z0-9_]{20,})', r'***', masked)
    masked = re.sub(r'(github_pat_[A-Za-z0-9_]{20,})', r'***', masked)
    masked = re.sub(r'(AIza[A-Za-z0-9_\-]{30,})', r'***', masked)
    return masked

class PathTraversalError(Exception):
    pass

class InvalidArchiveError(Exception):
    pass

@dataclass
class SnapshotResult:
    success: bool
    snapshot_path: Optional[str] = None
    environment_id: Optional[str] = None
    size_bytes: int = 0
    sha256: Optional[str] = None
    error_message: Optional[str] = None
    status_code: Optional[int] = None

@dataclass
class SnapshotInspectResult:
    is_valid: bool
    snapshot_path: str
    size_bytes: int = 0
    sha256: str = ""
    file_count: int = 0
    file_list: List[str] = field(default_factory=list)
    has_workspace: bool = False
    workspace_files: List[str] = field(default_factory=list)
    error_message: Optional[str] = None

@dataclass
class SnapshotRestoreResult:
    success: bool
    destination: str
    files_extracted: List[str] = field(default_factory=list)
    error_message: Optional[str] = None

class SnapshotManager:
    def __init__(self, key_index: int = 1):
        self.key_index = key_index

    def download_snapshot(self, environment_id: str, destination: str, timeout: int = 120) -> SnapshotResult:
        clean_env_id = environment_id.replace("environment-", "")
        api_key = get_api_key_by_index(self.key_index)
        if not api_key:
            return SnapshotResult(
                success=False,
                environment_id=clean_env_id,
                error_message=f"No API key available for index {self.key_index}."
            )

        url = f"https://generativelanguage.googleapis.com/v1beta/files/environment-{clean_env_id}:download"
        headers = {"x-goog-api-key": api_key}
        params = {"alt": "media"}

        try:
            res = requests.get(url, headers=headers, params=params, allow_redirects=True, timeout=timeout)
            if res.status_code != 200:
                return SnapshotResult(
                    success=False,
                    environment_id=clean_env_id,
                    status_code=res.status_code,
                    error_message=mask_credentials(f"HTTP Download failed with status {res.status_code}: {res.text[:200]}")
                )

            content = res.content
            # Validate if tar archive
            try:
                with tarfile.open(fileobj=io.BytesIO(content)) as tar:
                    pass
            except Exception as e:
                return SnapshotResult(
                    success=False,
                    environment_id=clean_env_id,
                    status_code=200,
                    error_message=f"Downloaded content is not a valid tar archive: {e}"
                )

            dest_path = os.path.abspath(destination)
            if os.path.isdir(dest_path):
                dest_file = os.path.join(dest_path, f"environment-{clean_env_id}.tar")
            else:
                dest_file = dest_path
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)

            with open(dest_file, "wb") as f:
                f.write(content)

            sha256_hash = hashlib.sha256(content).hexdigest()

            return SnapshotResult(
                success=True,
                snapshot_path=dest_file,
                environment_id=clean_env_id,
                size_bytes=len(content),
                sha256=sha256_hash,
                status_code=200
            )

        except Exception as e:
            return SnapshotResult(
                success=False,
                environment_id=clean_env_id,
                error_message=mask_credentials(f"Snapshot download request error: {str(e)}")
            )

    def inspect_snapshot(self, snapshot_path: str) -> SnapshotInspectResult:
        abs_path = os.path.abspath(snapshot_path)
        if not os.path.exists(abs_path):
            return SnapshotInspectResult(
                is_valid=False,
                snapshot_path=abs_path,
                error_message=f"Snapshot file not found: {abs_path}"
            )

        try:
            size_bytes = os.path.getsize(abs_path)
            sha256 = hashlib.sha256()
            with open(abs_path, "rb") as f:
                while chunk := f.read(8192):
                    sha256.update(chunk)
            sha256_hex = sha256.hexdigest()

            if not tarfile.is_tarfile(abs_path):
                return SnapshotInspectResult(
                    is_valid=False,
                    snapshot_path=abs_path,
                    size_bytes=size_bytes,
                    sha256=sha256_hex,
                    error_message="File is not a valid tar archive."
                )

            file_list = []
            workspace_files = []
            has_workspace = False

            with tarfile.open(abs_path, "r:*") as tar:
                for member in tar.getmembers():
                    name = member.name
                    file_list.append(name)
                    if "workspace" in name:
                        has_workspace = True
                        workspace_files.append(name)

            return SnapshotInspectResult(
                is_valid=True,
                snapshot_path=abs_path,
                size_bytes=size_bytes,
                sha256=sha256_hex,
                file_count=len(file_list),
                file_list=file_list,
                has_workspace=has_workspace,
                workspace_files=workspace_files
            )

        except Exception as e:
            return SnapshotInspectResult(
                is_valid=False,
                snapshot_path=abs_path,
                error_message=mask_credentials(f"Failed to inspect snapshot archive: {e}")
            )

    def _is_safe_path(self, destination: str, target_path: str) -> bool:
        dest_abs = os.path.abspath(destination)
        target_abs = os.path.abspath(target_path)
        return os.path.commonpath([dest_abs, target_abs]) == dest_abs

    def restore_snapshot(self, snapshot_path: str, destination: str) -> SnapshotRestoreResult:
        abs_snap = os.path.abspath(snapshot_path)
        dest_abs = os.path.abspath(destination)

        inspect = self.inspect_snapshot(abs_snap)
        if not inspect.is_valid:
            return SnapshotRestoreResult(
                success=False,
                destination=dest_abs,
                error_message=f"Cannot restore invalid snapshot: {inspect.error_message}"
            )

        os.makedirs(dest_abs, exist_ok=True)
        extracted = []

        try:
            with tarfile.open(abs_snap, "r:*") as tar:
                # Path Traversal Security Check
                for member in tar.getmembers():
                    target_path = os.path.join(dest_abs, member.name)
                    if not self._is_safe_path(dest_abs, target_path):
                        return SnapshotRestoreResult(
                            success=False,
                            destination=dest_abs,
                            error_message=f"INVALID_ARCHIVE_PATH: Detected path traversal attempt in archive member '{member.name}'"
                        )

                # Safely extract files after verification
                for member in tar.getmembers():
                    tar.extract(member, path=dest_abs)
                    extracted.append(member.name)

            return SnapshotRestoreResult(
                success=True,
                destination=dest_abs,
                files_extracted=extracted
            )

        except Exception as e:
            return SnapshotRestoreResult(
                success=False,
                destination=dest_abs,
                error_message=mask_credentials(f"Restore failed: {e}")
            )
