import os
import io
import re
import hashlib
import tempfile
import tarfile
import requests
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from keys import get_api_key_by_index
from git_manager import GitManager
from client import AntigravityClient

def mask_credentials(text: str) -> str:
    """Mask tokens, passwords, and API keys from logs/outputs."""
    if not text:
        return ""
    masked = re.sub(r'(https?://[^:\s]+:)([^@\s]+)(@)', r'\1***\3', text)
    masked = re.sub(r'(gh[pousr]_[A-Za-z0-9_]{20,})', r'***', masked)
    masked = re.sub(r'(github_pat_[A-Za-z0-9_]{20,})', r'***', masked)
    masked = re.sub(r'(AIza[A-Za-z0-9_\-]{30,})', r'***', masked)
    return masked

class SyncStatus(Enum):
    SYNC_SUCCESS = "SYNC_SUCCESS"
    SYNC_PARTIAL = "SYNC_PARTIAL"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    ENVIRONMENT_NOT_FOUND = "ENVIRONMENT_NOT_FOUND"
    TRANSFER_FAILED = "TRANSFER_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    CONFLICT = "CONFLICT"
    INVALID_PATH = "INVALID_PATH"
    ERROR = "ERROR"

@dataclass
class SourceManifest:
    source_type: str  # "local" or "git"
    source_path: str
    commit_sha: Optional[str] = None
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict) # rel_path -> {size, sha256, content_bytes}

@dataclass
class SyncResult:
    status: SyncStatus
    environment_id: Optional[str] = None
    source_path: Optional[str] = None
    commit_sha: Optional[str] = None
    synced_files: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)
    verified_files: List[str] = field(default_factory=list)
    output: str = ""
    error_message: Optional[str] = None

class WorkspaceSync:
    def __init__(self, key_index: int = 1):
        self.key_index = key_index

    def _is_safe_rel_path(self, rel_path: str) -> bool:
        if not rel_path or os.path.isabs(rel_path):
            return False
        normalized = os.path.normpath(rel_path)
        if normalized.startswith("..") or "/.." in normalized or "\\.." in normalized:
            return False
        return True

    def prepare_source_from_local(self, local_path: str) -> tuple[Optional[SourceManifest], Optional[str]]:
        abs_path = os.path.abspath(os.path.expanduser(local_path))
        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            return None, f"Source local path '{abs_path}' does not exist or is not a directory."

        manifest = SourceManifest(source_type="local", source_path=abs_path)

        for root, dirs, files in os.walk(abs_path):
            if ".git" in dirs:
                dirs.remove(".git")
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")

            for file in files:
                full_file_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_file_path, abs_path)

                if not self._is_safe_rel_path(rel_path):
                    return None, f"INVALID_PATH: Path traversal attempt detected in source file '{rel_path}'."

                try:
                    with open(full_file_path, "rb") as f:
                        data = f.read()
                    size = len(data)
                    sha256 = hashlib.sha256(data).hexdigest()
                    manifest.files[rel_path] = {
                        "full_path": full_file_path,
                        "size": size,
                        "sha256": sha256,
                        "content": data
                    }
                except Exception as e:
                    return None, f"Failed to read source file '{rel_path}': {e}"

        return manifest, None

    def prepare_source_from_git(self, repo_path: str, commit_sha: Optional[str] = None) -> tuple[Optional[SourceManifest], Optional[str]]:
        gm = GitManager(repo_path)
        if not gm.is_valid_repository():
            return None, f"Repository path '{repo_path}' is not a valid git repository."

        if commit_sha:
            res = gm._run_git(["rev-parse", "--verify", commit_sha])
            if res.returncode != 0:
                return None, f"Commit SHA '{commit_sha}' not found in repository '{repo_path}'."
            target_commit = res.stdout.strip()
        else:
            status = gm.get_status()
            target_commit = status.head_commit

        if not target_commit:
            return None, f"Could not determine commit SHA for repository '{repo_path}'."

        temp_dir = tempfile.mkdtemp()
        try:
            archive_res = gm._run_git(["archive", target_commit])
            if archive_res.returncode != 0:
                return None, f"Failed to archive git commit '{target_commit}': {archive_res.stderr}"

            raw_bytes = archive_res.stdout.encode('latin1') if isinstance(archive_res.stdout, str) else archive_res.stdout
            with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as tar:
                tar.extractall(path=temp_dir)

            manifest, err = self.prepare_source_from_local(temp_dir)
            if manifest:
                manifest.source_type = "git"
                manifest.source_path = repo_path
                manifest.commit_sha = target_commit
            return manifest, err
        except Exception as e:
            return None, f"Failed to prepare git source: {e}"
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def check_remote_conflicts(self, environment_id: str, manifest: SourceManifest) -> tuple[List[str], Optional[str]]:
        clean_env_id = environment_id.replace("environment-", "")
        api_key = get_api_key_by_index(self.key_index)
        if not api_key:
            return [], f"No API key available for index {self.key_index}."

        url = f"https://generativelanguage.googleapis.com/v1beta/environments/{clean_env_id}/files/workspace"
        headers = {"x-goog-api-key": api_key}

        try:
            res = requests.get(url, headers=headers, timeout=30)
            if res.status_code == 404:
                return [], None
            if res.status_code != 200:
                if res.status_code == 400 or "NOT_FOUND" in res.text:
                    return [], None
                return [], f"ENVIRONMENT_NOT_FOUND: Environment '{clean_env_id}' not found or inaccessible (HTTP {res.status_code})."

            data = res.json()
            remote_files = data.get("files", [])
            remote_paths = set()
            for rf in remote_files:
                r_path = rf.get("path", "")
                if r_path.startswith("workspace/"):
                    rel = r_path[10:]
                    remote_paths.add(rel)

            conflicts = []
            for rel_path in manifest.files.keys():
                if rel_path in remote_paths:
                    conflicts.append(rel_path)

            return conflicts, None
        except Exception as e:
            return [], mask_credentials(f"Error checking remote environment: {str(e)}")

    def sync_to_remote(
        self,
        environment_id: str,
        manifest: SourceManifest,
        overwrite: bool = False,
        destination: str = "/workspace"
    ) -> SyncResult:
        clean_env_id = environment_id.replace("environment-", "")
        api_key = get_api_key_by_index(self.key_index)
        if not api_key:
            return SyncResult(
                status=SyncStatus.ERROR,
                environment_id=clean_env_id,
                error_message=f"No API key available for index {self.key_index}."
            )

        conflicts, err = self.check_remote_conflicts(clean_env_id, manifest)
        if err and "ENVIRONMENT_NOT_FOUND" in err:
            return SyncResult(
                status=SyncStatus.ENVIRONMENT_NOT_FOUND,
                environment_id=clean_env_id,
                error_message=err
            )

        if conflicts and not overwrite:
            return SyncResult(
                status=SyncStatus.CONFLICT,
                environment_id=clean_env_id,
                skipped_files=conflicts,
                error_message=f"CONFLICT: {len(conflicts)} files already exist in remote /workspace ({', '.join(conflicts[:3])}). Use overwrite=True to replace."
            )

        client = AntigravityClient(api_key=api_key)

        prompt_lines = ["I need to sync project files into /workspace. Please execute the following setup carefully:\n"]
        for rel_path, info in manifest.files.items():
            if not self._is_safe_rel_path(rel_path):
                return SyncResult(
                    status=SyncStatus.INVALID_PATH,
                    environment_id=clean_env_id,
                    error_message=f"INVALID_PATH: Path traversal attempt detected in '{rel_path}'."
                )

            target_path = os.path.join(destination, rel_path)
            content_str = info["content"].decode("utf-8", errors="replace")
            
            prompt_lines.append(f"Write file '{target_path}':")
            prompt_lines.append("```")
            prompt_lines.append(content_str)
            prompt_lines.append("```\n")

        prompt_lines.append("After creating all files above, reply with exactly: WORKSPACE_SYNC_COMPLETE")
        full_prompt = "\n".join(prompt_lines)

        res = client.create_interaction(
            prompt=full_prompt,
            environment="remote",
            environment_id=clean_env_id
        )

        if not res.get("success"):
            return SyncResult(
                status=SyncStatus.TRANSFER_FAILED,
                environment_id=clean_env_id,
                error_message=mask_credentials(f"TRANSFER_FAILED: Interaction call failed with HTTP {res.get('status_code')}: {res.get('error')}")
            )

        synced_files = list(manifest.files.keys())

        verify_res = self.verify_remote(clean_env_id, manifest)
        if not verify_res["verified"]:
            return SyncResult(
                status=SyncStatus.VERIFY_FAILED,
                environment_id=clean_env_id,
                synced_files=synced_files,
                error_message=verify_res["error_message"]
            )

        return SyncResult(
            status=SyncStatus.SYNC_SUCCESS,
            environment_id=clean_env_id,
            source_path=manifest.source_path,
            commit_sha=manifest.commit_sha,
            synced_files=synced_files,
            verified_files=verify_res["verified_files"],
            output="Workspace sync completed and verified successfully."
        )

    def verify_remote(self, environment_id: str, manifest: SourceManifest) -> Dict[str, Any]:
        clean_env_id = environment_id.replace("environment-", "")
        api_key = get_api_key_by_index(self.key_index)
        if not api_key:
            return {
                "verified": False,
                "verified_files": [],
                "error_message": f"No API key available for index {self.key_index}."
            }
        
        client = AntigravityClient(api_key=api_key)
        
        verify_prompt = (
            "Please check the /workspace directory and list all files with their byte sizes and exact contents.\n"
            "Format your output as:\n"
            "FILE: <rel_path> SIZE: <bytes>\n"
            "End with: VERIFY_COMPLETE"
        )
        
        res = client.create_interaction(
            prompt=verify_prompt,
            environment="remote",
            environment_id=clean_env_id
        )

        if not res.get("success"):
            return {
                "verified": False,
                "verified_files": [],
                "error_message": mask_credentials(f"VERIFY_FAILED: Remote verification interaction call failed: {res.get('error')}")
            }

        output = res.get("output", "")
        verified_files = []

        for rel_path, info in manifest.files.items():
            filename = os.path.basename(rel_path)
            if filename in output or rel_path in output:
                verified_files.append(rel_path)

        if len(verified_files) < len(manifest.files):
            missing = set(manifest.files.keys()) - set(verified_files)
            return {
                "verified": False,
                "verified_files": verified_files,
                "error_message": f"VERIFY_FAILED: Missing {len(missing)} files in remote verification: {missing}"
            }

        return {
            "verified": True,
            "verified_files": verified_files,
            "error_message": None
        }
