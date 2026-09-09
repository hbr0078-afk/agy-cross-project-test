import os
import io
import re
import hashlib
import tarfile
import tempfile
import shutil
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

    def validate_archive_members(self, tar: tarfile.TarFile, destination: str) -> tuple[bool, Optional[str]]:
        """
        Thoroughly validates all tar members for path traversal, symlink/hardlink escapes,
        and nested symlink pivoting attacks before any extraction begins.
        """
        dest_abs = os.path.abspath(destination)
        known_symlinks = set()

        for member in tar.getmembers():
            # 1. Check member name path traversal
            target_path = os.path.join(dest_abs, member.name)
            if not self._is_safe_path(dest_abs, target_path):
                return False, f"INVALID_ARCHIVE_PATH: Detected path traversal attempt in member name '{member.name}'"

            # 2. Check hardlink attacks (LNKTYPE)
            if member.islnk():
                link_target = os.path.join(dest_abs, member.linkname) if not os.path.isabs(member.linkname) else member.linkname
                if not self._is_safe_path(dest_abs, link_target):
                    return False, f"INVALID_ARCHIVE_HARDLINK: Hardlink '{member.name}' points outside destination: '{member.linkname}'"

            # 3. Check symlink attacks (SYMTYPE)
            if member.issym():
                if os.path.isabs(member.linkname):
                    return False, f"INVALID_ARCHIVE_SYMLINK: Absolute symlink not allowed in member '{member.name}' -> '{member.linkname}'"
                
                # Resolve relative symlink from the member's parent directory
                member_dir = os.path.dirname(target_path)
                resolved_link = os.path.abspath(os.path.join(member_dir, member.linkname))
                if not self._is_safe_path(dest_abs, resolved_link):
                    return False, f"INVALID_ARCHIVE_SYMLINK: Symlink '{member.name}' points outside destination: '{member.linkname}'"

                known_symlinks.add(os.path.relpath(target_path, dest_abs))

            # 4. Check pivoting through existing symlink directories
            # If any parent directory in target_path is a registered symlink, reject to prevent pivot writing
            rel_name = os.path.relpath(target_path, dest_abs)
            parent = os.path.dirname(rel_name)
            while parent and parent != ".":
                if parent in known_symlinks:
                    return False, f"INVALID_ARCHIVE_SYMLINK_PIVOT: Path '{member.name}' traverses through symlink '{parent}'"
                parent = os.path.dirname(parent)

            # 5. Reject device files, FIFOs, etc.
            if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                return False, f"INVALID_ARCHIVE_TYPE: Unsupported special device file in member '{member.name}'"

        return True, None

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

        # Staging extraction in isolated temporary directory to ensure atomic / no-partial files on failure
        staging_dir = tempfile.mkdtemp(prefix="agy_restore_stage_")
        extracted = []

        try:
            with tarfile.open(abs_snap, "r:*") as tar:
                # Pre-validation across all members
                is_safe, err_msg = self.validate_archive_members(tar, staging_dir)
                if not is_safe:
                    return SnapshotRestoreResult(
                        success=False,
                        destination=dest_abs,
                        error_message=err_msg
                    )

                # Extract safely into staging_dir
                for member in tar.getmembers():
                    tar.extract(member, path=staging_dir)
                    extracted.append(member.name)

            # Move verified files into destination
            os.makedirs(dest_abs, exist_ok=True)
            for item in os.listdir(staging_dir):
                s_item = os.path.join(staging_dir, item)
                d_item = os.path.join(dest_abs, item)
                if os.path.islink(s_item):
                    link_target = os.readlink(s_item)
                    if os.path.lexists(d_item):
                        if os.path.isdir(d_item) and not os.path.islink(d_item):
                            shutil.rmtree(d_item)
                        else:
                            os.remove(d_item)
                    os.symlink(link_target, d_item)
                elif os.path.isdir(s_item):
                    if os.path.exists(d_item):
                        shutil.copytree(s_item, d_item, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copytree(s_item, d_item, symlinks=True)
                else:
                    shutil.copy2(s_item, d_item, follow_symlinks=False)

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
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
