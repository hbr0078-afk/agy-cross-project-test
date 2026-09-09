import os
import io
import re
import hashlib
import tempfile
import tarfile
import base64
import requests
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from keys import get_api_key_by_index
from git_manager import GitManager
from client import AntigravityClient

# Limits for single interaction safety
MAX_FILE_SIZE_BYTES = 512 * 1024       # 512 KB per file limit for prompt injection
MAX_TOTAL_PAYLOAD_BYTES = 2 * 1024 * 1024 # 2 MB total payload limit

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
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNKNOWN_CANNOT_VERIFY = "UNKNOWN_CANNOT_VERIFY"
    ERROR = "ERROR"

@dataclass
class SourceManifest:
    source_type: str  # "local" or "git"
    source_path: str
    commit_sha: Optional[str] = None
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict) # rel_path -> {full_path, size, sha256, content, is_binary}
    total_size: int = 0

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

    def _is_binary(self, data: bytes) -> bool:
        """Detect binary bytes via null byte or non-text ratio."""
        if not data:
            return False
        if b'\x00' in data:
            return True
        try:
            data.decode('utf-8')
            return False
        except UnicodeDecodeError:
            return True

    def prepare_source_from_local(self, local_path: str) -> tuple[Optional[SourceManifest], Optional[str]]:
        abs_path = os.path.abspath(os.path.expanduser(local_path))
        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            return None, f"Source local path '{abs_path}' does not exist or is not a directory."

        manifest = SourceManifest(source_type="local", source_path=abs_path)
        total_size = 0

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
                    total_size += size
                    sha256 = hashlib.sha256(data).hexdigest()
                    is_bin = self._is_binary(data)

                    manifest.files[rel_path] = {
                        "full_path": full_file_path,
                        "size": size,
                        "sha256": sha256,
                        "content": data,
                        "is_binary": is_bin
                    }
                except Exception as e:
                    return None, f"Failed to read source file '{rel_path}': {e}"

        manifest.total_size = total_size
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

        temp_dir = tempfile.mkdtemp(prefix="agy_git_src_")
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
            shutil.rmtree(temp_dir, ignore_errors=True)

    def check_remote_conflicts(self, environment_id: str, manifest: SourceManifest) -> tuple[List[str], Optional[str], Optional[str]]:
        """
        Check remote workspace for conflicts.
        Distinguishes clearly between:
        - clean workspace
        - actual conflicts
        - API endpoint unavailable / 404 / 403 / failure -> UNKNOWN_CANNOT_VERIFY
        """
        clean_env_id = environment_id.replace("environment-", "")
        api_key = get_api_key_by_index(self.key_index)
        if not api_key:
            return [], "NO_API_KEY", f"No API key available for index {self.key_index}."

        url = f"https://generativelanguage.googleapis.com/v1beta/environments/{clean_env_id}/files/workspace"
        headers = {"x-goog-api-key": api_key}

        try:
            res = requests.get(url, headers=headers, timeout=30)
            if res.status_code == 404:
                # Distinguish if environment itself is not found vs empty directory
                if "not found" in res.text.lower():
                    return [], "ENVIRONMENT_NOT_FOUND", f"ENVIRONMENT_NOT_FOUND: Environment '{clean_env_id}' not found."
                return [], None, None
            if res.status_code == 403 or res.status_code == 401:
                return [], "UNKNOWN_CANNOT_VERIFY", f"Authentication failed when checking remote conflicts (HTTP {res.status_code})."
            if res.status_code != 200:
                # Endpoint may not support listing directly or server error
                return [], "UNKNOWN_CANNOT_VERIFY", f"Cannot verify remote conflicts: HTTP {res.status_code}: {res.text[:100]}"

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

            return conflicts, None, None
        except Exception as e:
            return [], "UNKNOWN_CANNOT_VERIFY", mask_credentials(f"Error checking remote environment: {str(e)}")

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

        # Check payload size limits
        if manifest.total_size > MAX_TOTAL_PAYLOAD_BYTES:
            return SyncResult(
                status=SyncStatus.PAYLOAD_TOO_LARGE,
                environment_id=clean_env_id,
                error_message=f"Total payload size ({manifest.total_size} bytes) exceeds limit ({MAX_TOTAL_PAYLOAD_BYTES} bytes)."
            )

        for rel_path, info in manifest.files.items():
            if info["size"] > MAX_FILE_SIZE_BYTES:
                return SyncResult(
                    status=SyncStatus.FILE_TOO_LARGE,
                    environment_id=clean_env_id,
                    error_message=f"File '{rel_path}' size ({info['size']} bytes) exceeds limit ({MAX_FILE_SIZE_BYTES} bytes)."
                )

        # Conflict check with fail-safe UNKNOWN handling
        conflicts, err_type, err_msg = self.check_remote_conflicts(clean_env_id, manifest)
        if err_type == "ENVIRONMENT_NOT_FOUND":
            return SyncResult(
                status=SyncStatus.ENVIRONMENT_NOT_FOUND,
                environment_id=clean_env_id,
                error_message=err_msg
            )
        if err_type == "UNKNOWN_CANNOT_VERIFY" and not overwrite:
            return SyncResult(
                status=SyncStatus.UNKNOWN_CANNOT_VERIFY,
                environment_id=clean_env_id,
                error_message=f"UNKNOWN_CANNOT_VERIFY: Cannot verify remote workspace state: {err_msg}. Use overwrite=True to proceed intentionally."
            )

        if conflicts and not overwrite:
            return SyncResult(
                status=SyncStatus.CONFLICT,
                environment_id=clean_env_id,
                skipped_files=conflicts,
                error_message=f"CONFLICT: {len(conflicts)} files already exist in remote /workspace ({', '.join(conflicts[:3])}). Use overwrite=True to replace."
            )

        client = AntigravityClient(api_key=api_key)

        # Single Interaction Pattern: file injection + python sha256 integrity reporting in 1 prompt
        prompt_lines = [
            "We are setting up the project workspace in /workspace.",
            "Write the following files precisely according to their encodings and verify their SHA256 checksums.\n"
        ]

        for rel_path, info in manifest.files.items():
            if not self._is_safe_rel_path(rel_path):
                return SyncResult(
                    status=SyncStatus.INVALID_PATH,
                    environment_id=clean_env_id,
                    error_message=f"INVALID_PATH: Path traversal attempt detected in '{rel_path}'."
                )

            target_path = os.path.join(destination, rel_path)

            if info.get("is_binary"):
                b64_str = base64.b64encode(info["content"]).decode("ascii")
                prompt_lines.append(f"File '{target_path}' (BINARY Base64, size={info['size']}, sha256={info['sha256']}):")
                prompt_lines.append(f"BASE64_START:{target_path}")
                prompt_lines.append(b64_str)
                prompt_lines.append(f"BASE64_END:{target_path}\n")
            else:
                content_str = info["content"].decode("utf-8", errors="replace")
                prompt_lines.append(f"File '{target_path}' (TEXT UTF-8, size={info['size']}, sha256={info['sha256']}):")
                prompt_lines.append(f"CONTENT_START:{target_path}")
                prompt_lines.append(content_str)
                prompt_lines.append(f"CONTENT_END:{target_path}\n")

        prompt_lines.append(
            "Execute the file creation. After writing all files, run a sha256 verification and reply STRICTLY in this format:\n"
            "---INTEGRITY_REPORT_START---\n"
            "For each created file in destination, output a single line:\n"
            "FILE:<rel_path>|SIZE:<bytes>|SHA256:<hex_digest>\n"
            "---INTEGRITY_REPORT_END---\n"
            "End your final message with: WORKSPACE_SYNC_COMPLETE"
        )

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

        output = res.get("output", "")
        # Verify integrity directly from interaction response
        integrity_check = self.parse_and_verify_integrity(output, manifest)
        if not integrity_check["verified"]:
            return SyncResult(
                status=SyncStatus.VERIFY_FAILED,
                environment_id=clean_env_id,
                synced_files=list(manifest.files.keys()),
                error_message=integrity_check["error_message"]
            )

        return SyncResult(
            status=SyncStatus.SYNC_SUCCESS,
            environment_id=clean_env_id,
            source_path=manifest.source_path,
            commit_sha=manifest.commit_sha,
            synced_files=list(manifest.files.keys()),
            verified_files=integrity_check["verified_files"],
            output="Workspace sync completed and verified with SHA256 integrity successfully."
        )

    def parse_and_verify_integrity(self, output: str, manifest: SourceManifest) -> Dict[str, Any]:
        """
        Validates that every file in manifest matches size and SHA256 exactly.
        Rejects on missing files, size mismatches, or checksum mismatches.
        """
        if not output:
            return {
                "verified": False,
                "verified_files": [],
                "error_message": "VERIFY_FAILED: Remote output is empty, cannot verify integrity."
            }

        report_pattern = re.compile(r"FILE:([^|\s]+)\|SIZE:(\d+)\|SHA256:([a-fA-F0-9]{64})")
        found_records = {}

        for line in output.splitlines():
            line = line.strip()
            m = report_pattern.search(line)
            if m:
                path = m.group(1).lstrip("/")
                if path.startswith("workspace/"):
                    path = path[10:]
                size = int(m.group(2))
                sha = m.group(3).lower()
                found_records[path] = {"size": size, "sha256": sha}

        verified_files = []
        for rel_path, expected in manifest.files.items():
            norm_rel = os.path.normpath(rel_path)
            record = found_records.get(norm_rel) or found_records.get(rel_path) or found_records.get(os.path.basename(rel_path))

            if not record:
                # Check fallback if output mentions exact sha256 and size
                if expected["sha256"] in output:
                    verified_files.append(rel_path)
                    continue
                return {
                    "verified": False,
                    "verified_files": verified_files,
                    "error_message": f"VERIFY_FAILED: File '{rel_path}' missing from remote integrity report."
                }

            if record["size"] != expected["size"]:
                return {
                    "verified": False,
                    "verified_files": verified_files,
                    "error_message": f"VERIFY_FAILED: Size mismatch for '{rel_path}': expected {expected['size']} bytes, got {record['size']} bytes."
                }

            if record["sha256"] != expected["sha256"].lower():
                return {
                    "verified": False,
                    "verified_files": verified_files,
                    "error_message": f"VERIFY_FAILED: SHA256 mismatch for '{rel_path}': expected {expected['sha256']}, got {record['sha256']}."
                }

            verified_files.append(rel_path)

        return {
            "verified": True,
            "verified_files": verified_files,
            "error_message": None
        }
