import os
import io
import re
import hashlib
import tempfile
import tarfile
import base64
import shutil
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
        if normalized == ".." or normalized.startswith(".." + os.sep) or normalized.startswith("../") or "/../" in normalized or "\\..\\" in normalized:
            return False
        if normalized.startswith(".."):
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
            archive_res = gm._run_git_bytes(["archive", target_commit])
            if archive_res.returncode != 0:
                return None, f"Failed to archive git commit '{target_commit}': {archive_res.stderr}"

            raw_bytes = archive_res.stdout
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
        - clean workspace: ([], None, None)
        - actual conflicts: (conflicts, None, None)
        - non-existent environment: ([], "ENVIRONMENT_NOT_FOUND", msg)
        - API failure / auth error: ([], "UNKNOWN_CANNOT_VERIFY", msg)
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
                err_code = None
                err_msg = ""
                try:
                    err_json = res.json()
                    err_obj = err_json.get("error", {})
                    if isinstance(err_obj, dict):
                        err_code = str(err_obj.get("code", "")).lower()
                        err_msg = str(err_obj.get("message", "")).lower()
                    elif isinstance(err_obj, str):
                        err_msg = err_obj.lower()
                except Exception:
                    pass

                # Fallback to text inspection if JSON parsing yielded no message
                raw_text = (err_msg or res.text or "").lower()
                env_not_found_patterns = [
                    f"environment '{clean_env_id}' not found",
                    "environment not found",
                    f"environment {clean_env_id} not found",
                ]
                if any(p in raw_text for p in env_not_found_patterns):
                    return [], "ENVIRONMENT_NOT_FOUND", f"ENVIRONMENT_NOT_FOUND: Environment '{clean_env_id}' not found."
                # If path 'workspace' or other subresource not found, it means workspace folder is empty/clean
                return [], None, None
            if res.status_code == 400:
                return [], "ENVIRONMENT_NOT_FOUND", f"ENVIRONMENT_NOT_FOUND: Environment '{clean_env_id}' not found or invalid."
            if res.status_code in (401, 403):
                return [], "UNKNOWN_CANNOT_VERIFY", f"Authentication failed when checking remote conflicts (HTTP {res.status_code})."
            if res.status_code != 200:
                return [], "UNKNOWN_CANNOT_VERIFY", f"Cannot verify remote conflicts: HTTP {res.status_code}: {res.text[:100]}"

            data = res.json()
            remote_files = data.get("files", [])
            remote_paths = set()
            for rf in remote_files:
                p = rf.get("name", "")
                if p.startswith("/workspace/"):
                    p = p[len("/workspace/"):]
                remote_paths.add(p)

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
        overwrite: bool = False
    ) -> SyncResult:
        """
        Synchronizes files from SourceManifest into the remote Antigravity environment.
        - Validates paths first (fails fast on path traversal)
        - Checks payload & file size boundaries
        - Checks conflicts / fail-safe on unverified remote state
        - Injects files & verifies integrity via SHA256 in single interaction
        """
        clean_env_id = environment_id.replace("environment-", "")

        # 1. Path safety check on all manifest files first (Fail-fast)
        for rel_path in manifest.files.keys():
            if not self._is_safe_rel_path(rel_path):
                return SyncResult(
                    status=SyncStatus.INVALID_PATH,
                    environment_id=clean_env_id,
                    error_message=f"INVALID_PATH: Path traversal attempt detected in path '{rel_path}'."
                )

        api_key = get_api_key_by_index(self.key_index)
        if not api_key:
            return SyncResult(
                status=SyncStatus.ERROR,
                environment_id=clean_env_id,
                error_message=f"No API key available for index {self.key_index}."
            )

        # 2. Check payload size limits (Fail-fast)
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
                    error_message=f"File '{rel_path}' size ({info['size']} bytes) exceeds per-file limit ({MAX_FILE_SIZE_BYTES} bytes)."
                )

        # 3. Remote conflict check with fail-safe unpacking
        conflict_res = self.check_remote_conflicts(clean_env_id, manifest)
        if len(conflict_res) == 2:
            conflicts, err_msg = conflict_res
            err_type = "ERROR" if err_msg else None
        else:
            conflicts, err_type, err_msg = conflict_res

        if err_type == "ENVIRONMENT_NOT_FOUND":
            return SyncResult(
                status=SyncStatus.ENVIRONMENT_NOT_FOUND,
                environment_id=clean_env_id,
                error_message=err_msg or f"Environment '{clean_env_id}' not found."
            )
        elif err_type == "UNKNOWN_CANNOT_VERIFY" or (err_msg and not conflicts):
            return SyncResult(
                status=SyncStatus.UNKNOWN_CANNOT_VERIFY if err_type == "UNKNOWN_CANNOT_VERIFY" else SyncStatus.ERROR,
                environment_id=clean_env_id,
                error_message=err_msg or "Cannot verify remote state."
            )

        if conflicts and not overwrite:
            return SyncResult(
                status=SyncStatus.CONFLICT,
                environment_id=clean_env_id,
                skipped_files=conflicts,
                error_message=f"CONFLICT: {len(conflicts)} files already exist on remote. Set overwrite=True to overwrite."
            )

        # 4. Construct payload for single interaction injection & verification
        python_unpack_lines = [
            "import os, base64, hashlib",
            "os.makedirs('/workspace', exist_ok=True)",
            "files_data = {"
        ]

        for rel_path, info in manifest.files.items():
            b64_content = base64.b64encode(info["content"]).decode("ascii")
            python_unpack_lines.append(f"    {repr(rel_path)}: {repr(b64_content)},")

        python_unpack_lines.extend([
            "}",
            "print('---INTEGRITY_REPORT_START---')",
            "for rel_path, b64_str in files_data.items():",
            "    target = os.path.join('/workspace', rel_path)",
            "    os.makedirs(os.path.dirname(target), exist_ok=True)",
            "    raw = base64.b64decode(b64_str)",
            "    with open(target, 'wb') as f: f.write(raw)",
            "    size = len(raw)",
            "    sha = hashlib.sha256(raw).hexdigest()",
            "    print(f'FILE:{rel_path}|SIZE:{size}|SHA256:{sha}')",
            "print('---INTEGRITY_REPORT_END---')",
            "print('WORKSPACE_SYNC_COMPLETE')"
        ])

        script_body = "\n".join(python_unpack_lines)
        prompt = (
            f"Please run the following python synchronization script to populate /workspace:\n"
            f"```python\n{script_body}\n```\n"
            f"Execute it and print the output exactly."
        )

        client = AntigravityClient(api_key=api_key)
        res = client.create_interaction(
            prompt=prompt,
            environment="remote",
            environment_id=clean_env_id
        )

        if not res.get("success"):
            status_code = res.get("status_code", 0)
            err = res.get("error", "Unknown error")
            if status_code == 404 or "not found" in str(err).lower():
                sync_status = SyncStatus.ENVIRONMENT_NOT_FOUND
            else:
                sync_status = SyncStatus.TRANSFER_FAILED

            return SyncResult(
                status=sync_status,
                environment_id=clean_env_id,
                error_message=mask_credentials(f"Transfer failed: {err}")
            )

        output = res.get("output", "")
        verification = self.parse_and_verify_integrity(output, manifest)

        if not verification.get("verified"):
            return SyncResult(
                status=SyncStatus.VERIFY_FAILED,
                environment_id=clean_env_id,
                synced_files=list(manifest.files.keys()),
                verified_files=verification.get("verified_files", []),
                error_message=mask_credentials(f"VERIFY_FAILED: {verification.get('error_message')}")
            )

        return SyncResult(
            status=SyncStatus.SYNC_SUCCESS,
            environment_id=clean_env_id,
            source_path=manifest.source_path,
            commit_sha=manifest.commit_sha,
            synced_files=list(manifest.files.keys()),
            verified_files=verification.get("verified_files", []),
            output=output
        )

    def parse_and_verify_integrity(self, report_text: str, manifest: SourceManifest) -> Dict[str, Any]:
        """
        Strictly parses structured integrity report:
        ---INTEGRITY_REPORT_START---
        FILE:<rel_path>|SIZE:<size>|SHA256:<sha256>
        ---INTEGRITY_REPORT_END---
        Verifies exact relative path, file size, and sha256. Fails on spoofed/extra/missing data.
        """
        if not report_text:
            return {"verified": False, "verified_files": [], "error_message": "Empty integrity report"}

        start_marker = "---INTEGRITY_REPORT_START---"
        end_marker = "---INTEGRITY_REPORT_END---"

        if start_marker not in report_text or end_marker not in report_text:
            return {"verified": False, "verified_files": [], "error_message": "Malformed remote response: missing integrity markers"}

        try:
            section = report_text.split(start_marker, 1)[1].split(end_marker, 1)[0]
        except Exception:
            return {"verified": False, "verified_files": [], "error_message": "Malformed remote response: cannot slice integrity section"}

        reported_files = {}
        for line in section.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^FILE:(.+?)\|SIZE:(\d+)\|SHA256:([a-fA-F0-9]+)$", line)
            if not m:
                return {"verified": False, "verified_files": [], "error_message": f"Malformed integrity report line: '{line}'"}

            r_path, r_size, r_sha = m.group(1).strip(), int(m.group(2)), m.group(3).lower()
            if r_path in reported_files:
                return {"verified": False, "verified_files": [], "error_message": f"Duplicate file reported in integrity report: '{r_path}'"}
            reported_files[r_path] = {"size": r_size, "sha256": r_sha}

        # Ensure no extra unexpected files
        for r_path in reported_files.keys():
            if r_path not in manifest.files:
                return {
                    "verified": False,
                    "verified_files": [],
                    "error_message": f"Unexpected extra file reported on remote: '{r_path}'"
                }

        verified_files = []
        for rel_path, expected in manifest.files.items():
            if rel_path not in reported_files:
                return {
                    "verified": False,
                    "verified_files": verified_files,
                    "error_message": f"Missing file in remote report: '{rel_path}'"
                }

            actual = reported_files[rel_path]
            if actual["size"] != expected["size"]:
                return {
                    "verified": False,
                    "verified_files": verified_files,
                    "error_message": f"Size mismatch for '{rel_path}': expected {expected['size']}, got {actual['size']}"
                }

            if actual["sha256"].lower() != expected["sha256"].lower():
                return {
                    "verified": False,
                    "verified_files": verified_files,
                    "error_message": f"SHA256 mismatch for '{rel_path}': expected {expected['sha256']}, got {actual['sha256']}"
                }

            verified_files.append(rel_path)

        return {"verified": True, "verified_files": verified_files, "error_message": None}
