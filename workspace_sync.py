import os
import io
import re
import hashlib
import tempfile
import tarfile
import base64
import shutil
import requests
import mimetypes
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from keys import KeyPoolManager, AllKeysExhaustedError, get_api_key_by_index
from git_manager import GitManager
from client import AntigravityClient
from environment_files import (
    EnvironmentFileClient, SyncPlanner, SyncAction, 
    IntegrityVerifier, RemoteManifest, PlannedFileAction, RemoteFileInfo
)
from sessions import SessionStateManager

# Transport safety limits (configurable, not claims of hard Google limits)
DEFAULT_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024       # 10 MB default
DEFAULT_MAX_TOTAL_SYNC_BYTES = 50 * 1024 * 1024      # 50 MB default

# Backward compatibility alias for legacy tests
MAX_FILE_SIZE_BYTES = DEFAULT_MAX_FILE_SIZE_BYTES
MAX_TOTAL_PAYLOAD_BYTES = DEFAULT_MAX_TOTAL_SYNC_BYTES

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
    failed_files: List[str] = field(default_factory=list)
    output: str = ""
    error_message: Optional[str] = None

class WorkspaceSync:
    def __init__(
        self,
        key_index: int = 1,
        key_pool: Optional[KeyPoolManager] = None,
        project_id: Optional[str] = None,
        registry: Optional[Any] = None,
        session_manager: Optional[SessionStateManager] = None,
        session_id: Optional[str] = None,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        max_total_sync_bytes: int = DEFAULT_MAX_TOTAL_SYNC_BYTES,
    ):
        self.key_index = key_index
        self.key_pool = key_pool
        self.project_id = project_id
        self.registry = registry
        self.session_manager = session_manager
        self.session_id = session_id
        self.max_file_size_bytes = max_file_size_bytes
        self.max_total_sync_bytes = max_total_sync_bytes

    @property
    def is_pool_mode(self) -> bool:
        return self.key_pool is not None

    def _is_safe_rel_path(self, rel_path: str) -> bool:
        if not rel_path or os.path.isabs(rel_path):
            return False
        if "\x00" in rel_path:
            return False
        normalized = os.path.normpath(rel_path).replace("\\", "/")
        if normalized == "." or normalized == ".." or normalized.startswith("../") or "/../" in normalized or normalized.endswith("/.."):
            return False
        if normalized.startswith("/"):
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

    def _execute_conflict_check_request(
        self,
        clean_env_id: str,
        api_key: str,
        timeout: int = 30
    ) -> tuple[int, Optional[Dict[str, Any]], Optional[str]]:
        """
        Executes raw GET request for workspace files via Environment File API.
        Returns (status_code, response_json_or_None, error_text_or_None).
        """
        url = f"https://generativelanguage.googleapis.com/v1beta/environments/{clean_env_id}/files/workspace"
        headers = {"x-goog-api-key": api_key}
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            status_code = res.status_code
            if status_code == 200:
                try:
                    return status_code, res.json(), None
                except Exception as e:
                    return status_code, None, str(e)
            else:
                err_text = ""
                try:
                    err_json = res.json()
                    err_obj = err_json.get("error", {})
                    if isinstance(err_obj, dict):
                        err_text = str(err_obj.get("message", ""))
                    elif isinstance(err_obj, str):
                        err_text = err_obj
                except Exception:
                    err_text = res.text[:200]
                return status_code, None, err_text or res.text[:200]
        except requests.exceptions.RequestException as e:
            return 0, None, str(e)

    def _parse_conflict_data(
        self,
        clean_env_id: str,
        manifest: SourceManifest,
        status_code: int,
        data: Optional[Dict[str, Any]],
        err_text: Optional[str]
    ) -> tuple[List[str], Optional[str], Optional[str]]:
        if status_code == 404:
            raw_text = (err_text or "").lower()
            env_not_found_patterns = [
                f"environment '{clean_env_id}' not found",
                "environment not found",
                f"environment {clean_env_id} not found",
            ]
            if any(p in raw_text for p in env_not_found_patterns):
                return [], "ENVIRONMENT_NOT_FOUND", f"ENVIRONMENT_NOT_FOUND: Environment '{clean_env_id}' not found."
            # If path 'workspace' not found, workspace is empty/clean
            return [], None, None

        if status_code == 400:
            return [], "ENVIRONMENT_NOT_FOUND", f"ENVIRONMENT_NOT_FOUND: Environment '{clean_env_id}' not found or invalid."

        if status_code in (401, 403):
            return [], "UNKNOWN_CANNOT_VERIFY", f"Authentication failed when checking remote conflicts (HTTP {status_code})."

        if status_code != 200 or data is None:
            return [], "UNKNOWN_CANNOT_VERIFY", f"Cannot verify remote conflicts: HTTP {status_code}: {err_text or ''}"

        remote_files = data.get("files", [])
        remote_entries = {}
        for rf in remote_files:
            r_path = rf.get("path", "")
            if r_path.startswith("workspace/"):
                p = r_path[len("workspace/"):]
            elif r_path.startswith("/workspace/"):
                p = r_path[len("/workspace/"):]
            else:
                p = rf.get("name", "")
                if p.startswith("/workspace/"):
                    p = p[len("/workspace/"):]
                elif p.startswith("workspace/"):
                    p = p[len("workspace/"):]

            p = os.path.normpath(p)
            size = None
            if "size_bytes" in rf:
                try:
                    size = int(rf["size_bytes"])
                except (ValueError, TypeError):
                    pass
            remote_entries[p] = {
                "size": size,
                "sha256": rf.get("sha256") or rf.get("checksum"),
                "raw": rf
            }

        conflicts = []
        for rel_path, local_info in manifest.files.items():
            norm_rel = os.path.normpath(rel_path)
            if norm_rel in remote_entries:
                remote_meta = remote_entries[norm_rel]
                if remote_meta.get("sha256"):
                    if remote_meta["sha256"].lower() != local_info["sha256"].lower():
                        conflicts.append(rel_path)
                else:
                    conflicts.append(rel_path)

        return conflicts, None, None

    def check_remote_conflicts(self, environment_id: str, manifest: SourceManifest) -> tuple[List[str], Optional[str], Optional[str]]:
        clean_env_id = EnvironmentFileClient.clean_environment_id(environment_id)

        # 1. Single-key legacy mode
        if not self.is_pool_mode:
            api_key = get_api_key_by_index(self.key_index)
            if not api_key:
                return [], "NO_API_KEY", f"No API key available for index {self.key_index}."

            status_code, data, err_text = self._execute_conflict_check_request(clean_env_id, api_key)
            return self._parse_conflict_data(clean_env_id, manifest, status_code, data, err_text)

        # 2. KeyPool mode with session safety
        assert self.key_pool is not None

        # Check session bound key first
        target_session = None
        if self.session_manager and (self.session_id or self.project_id):
            sid = self.session_id or (f"sess_{self.project_id}" if self.project_id else None)
            if sid:
                target_session = self.session_manager.get_session(sid)

        if target_session and target_session.get("bound_key"):
            bound_ref = target_session["bound_key"]
            raw_key = None
            m = re.match(r"key(\d+)", bound_ref)
            if m:
                raw_key = get_api_key_by_index(int(m.group(1)))
            if raw_key:
                status_code, res_json, err_text = self._execute_conflict_check_request(clean_env_id, raw_key)
                if status_code == 200:
                    self.key_pool.report_result(bound_ref, status_code=200)
                return self._parse_conflict_data(clean_env_id, manifest, status_code, res_json, err_text)

        # Discovered keys check
        discovered_keys = self.key_pool.discover_keys()
        total_keys = len(discovered_keys) if discovered_keys else 1
        max_attempts = max(total_keys * 2, 4)

        tried_keys = set()
        same_key_retried = set()
        excluded_keys = set()
        attempts = 0
        last_parsed = None

        while attempts < max_attempts:
            attempts += 1
            try:
                prefer_candidate = None
                if self.project_id and self.registry:
                    try:
                        p = self.registry.get_project(self.project_id)
                        if p and p.get("active_key") and p["active_key"] not in excluded_keys:
                            prefer_candidate = p["active_key"]
                    except Exception:
                        pass

                if not prefer_candidate:
                    data_status = self.key_pool.get_status()
                    candidates = [
                        k for k, v in data_status.items()
                        if v.get("state") == "ACTIVE" and k not in excluded_keys
                    ]
                    prefer_candidate = candidates[0] if candidates else None

                key_ref, key_idx = self.key_pool.acquire_key(
                    prefer_key=prefer_candidate,
                    project_id=self.project_id,
                    registry=self.registry
                )
            except AllKeysExhaustedError:
                raise

            tried_keys.add(key_ref)
            raw_key = get_api_key_by_index(key_idx)
            if not raw_key:
                self.key_pool.report_result(key_ref, status_code=401)
                excluded_keys.add(key_ref)
                continue

            status_code, res_json, err_text = self._execute_conflict_check_request(clean_env_id, raw_key)
            parsed_result = self._parse_conflict_data(clean_env_id, manifest, status_code, res_json, err_text)
            last_parsed = parsed_result

            if status_code == 200:
                self.key_pool.report_result(key_ref, status_code=200)
                if self.project_id and self.registry:
                    try:
                        self.registry.update_project_state(
                            project_id=self.project_id,
                            active_key=key_ref
                        )
                    except Exception:
                        pass
                return parsed_result

            if status_code is not None and 400 <= status_code < 500 and status_code not in (401, 403, 429):
                self.key_pool.report_result(key_ref, status_code=status_code)
                return parsed_result

            if status_code == 429:
                self.key_pool.report_result(key_ref, status_code=429)
                excluded_keys.add(key_ref)
                continue

            if status_code == 401:
                self.key_pool.report_result(key_ref, status_code=401)
                excluded_keys.add(key_ref)
                continue

            if status_code == 403:
                self.key_pool.report_result(key_ref, status_code=403)
                excluded_keys.add(key_ref)
                continue

            if status_code is not None and status_code in (500, 502, 503, 504):
                if key_ref not in same_key_retried:
                    same_key_retried.add(key_ref)
                    continue
                self.key_pool.report_result(key_ref, status_code=status_code)
                excluded_keys.add(key_ref)
                continue

            if status_code == 0 or status_code is None:
                if key_ref not in same_key_retried:
                    same_key_retried.add(key_ref)
                    continue
                self.key_pool.report_result(key_ref, status_code=None)
                excluded_keys.add(key_ref)
                continue

            self.key_pool.report_result(key_ref, status_code=status_code)
            excluded_keys.add(key_ref)

        if last_parsed is not None:
            return last_parsed
        return [], "UNKNOWN_CANNOT_VERIFY", "Conflict check failed: maximum attempts reached"

    def _resolve_effective_key(self, environment_id: str) -> tuple[Optional[str], Optional[str]]:
        """
        Resolves the effective API key and key reference, preserving tenant isolation.
        Returns (raw_key, key_ref).
        """
        # Session mode has highest priority for tenant isolation
        if self.session_manager and (self.session_id or self.project_id):
            sid = self.session_id or (f"sess_{self.project_id}" if self.project_id else None)
            if sid:
                sess = self.session_manager.get_session(sid)
                if sess and sess.get("bound_key"):
                    b_ref = sess["bound_key"]
                    m = re.match(r"key(\d+)", b_ref)
                    if m:
                        k = get_api_key_by_index(int(m.group(1)))
                        if k:
                            return k, b_ref

        if not self.is_pool_mode:
            raw = get_api_key_by_index(self.key_index)
            return raw, f"key{self.key_index}"

        # Pool mode without session
        assert self.key_pool is not None
        key_ref, key_idx = self.key_pool.acquire_key(
            project_id=self.project_id,
            registry=self.registry
        )
        return get_api_key_by_index(key_idx), key_ref

    def sync_to_remote(
        self,
        environment_id: str,
        manifest: SourceManifest,
        overwrite: bool = False
    ) -> SyncResult:
        """
        Direct Environment File API Synchronizer (Phase 7-3).
        - Validates all local paths against path traversal
        - Checks payload & file size safety limits
        - Resolves effective key with strict tenant safety
        - Lists remote files via EnvironmentFileClient and follows pagination
        - Builds SyncPlan via SyncPlanner (handles exact diff and conflicts)
        - Uploads new / modified files directly via PUT Environment File API
        - Verifies each uploaded file's integrity (alt=media SHA256 & size check)
        - Reports detailed SyncResult (synced, skipped, verified, failed)
        """
        clean_env_id = EnvironmentFileClient.clean_environment_id(environment_id)

        # 1. Path safety check on all manifest files first (Fail-fast)
        for rel_path in manifest.files.keys():
            if not self._is_safe_rel_path(rel_path):
                return SyncResult(
                    status=SyncStatus.INVALID_PATH,
                    environment_id=clean_env_id,
                    error_message=f"INVALID_PATH: Path traversal attempt detected in path '{rel_path}'."
                )

        # 2. Check transport safety limits
        if manifest.total_size > self.max_total_sync_bytes:
            return SyncResult(
                status=SyncStatus.PAYLOAD_TOO_LARGE,
                environment_id=clean_env_id,
                error_message=f"Total payload size ({manifest.total_size} bytes) exceeds limit ({self.max_total_sync_bytes} bytes)."
            )

        for rel_path, info in manifest.files.items():
            if info["size"] > self.max_file_size_bytes:
                return SyncResult(
                    status=SyncStatus.FILE_TOO_LARGE,
                    environment_id=clean_env_id,
                    error_message=f"File '{rel_path}' size ({info['size']} bytes) exceeds per-file limit ({self.max_file_size_bytes} bytes)."
                )

        # 3. Resolve effective API Key
        raw_key, key_ref = self._resolve_effective_key(clean_env_id)
        if not raw_key:
            return SyncResult(
                status=SyncStatus.ERROR,
                environment_id=clean_env_id,
                error_message="No usable API key found to perform sync."
            )

        file_client = EnvironmentFileClient(api_key=raw_key)

        # 4. List remote files with pagination & build remote manifest
        try:
            remote_file_list = file_client.list_all_files(clean_env_id, base_path="workspace")
        except FileNotFoundError as e:
            if self.is_pool_mode and self.key_pool and key_ref:
                self.key_pool.report_result(key_ref, status_code=404)
            return SyncResult(
                status=SyncStatus.ENVIRONMENT_NOT_FOUND,
                environment_id=clean_env_id,
                error_message=mask_credentials(f"Environment '{clean_env_id}' not found: {e}")
            )
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 500
            if self.is_pool_mode and self.key_pool and key_ref:
                self.key_pool.report_result(key_ref, status_code=code)
            if code == 404:
                return SyncResult(
                    status=SyncStatus.ENVIRONMENT_NOT_FOUND,
                    environment_id=clean_env_id,
                    error_message=mask_credentials(f"Environment '{clean_env_id}' not found.")
                )
            if code in (401, 403):
                return SyncResult(
                    status=SyncStatus.UNKNOWN_CANNOT_VERIFY,
                    environment_id=clean_env_id,
                    error_message=mask_credentials(f"Authentication failure (HTTP {code}).")
                )
            return SyncResult(
                status=SyncStatus.TRANSFER_FAILED,
                environment_id=clean_env_id,
                error_message=mask_credentials(f"Failed to list remote workspace files: {e}")
            )
        except Exception as e:
            return SyncResult(
                status=SyncStatus.TRANSFER_FAILED,
                environment_id=clean_env_id,
                error_message=mask_credentials(f"Error querying remote workspace: {e}")
            )

        # 5. Build Sync Plan
        remote_manifest = SyncPlanner.build_remote_manifest(clean_env_id, remote_file_list, base_dir="workspace")
        plan = SyncPlanner.plan(
            source_manifest=manifest,
            remote_manifest=remote_manifest,
            file_client=file_client,
            overwrite=overwrite
        )

        if plan.has_conflicts and not overwrite:
            return SyncResult(
                status=SyncStatus.CONFLICT,
                environment_id=clean_env_id,
                skipped_files=plan.conflicts,
                error_message=f"CONFLICT: {len(plan.conflicts)} files already exist on remote. Set overwrite=True to overwrite."
            )

        # 6. Execute Upload Plan
        synced_files: List[str] = []
        skipped_files: List[str] = []
        failed_files: List[str] = []
        verified_files: List[str] = []
        verifier = IntegrityVerifier(file_client)

        for file_action in plan.actions:
            rel_p = file_action.rel_path
            if file_action.action == SyncAction.UNCHANGED:
                skipped_files.append(rel_p)
                continue
            if file_action.action == SyncAction.REMOTE_ONLY:
                continue

            if file_action.action in (SyncAction.UPLOAD, SyncAction.UPDATE):
                file_info = manifest.files.get(rel_p)
                if not file_info:
                    continue
                content = file_info["content"]
                target_remote_path = f"workspace/{rel_p}"

                upload_ok = False
                # Per-file retry policy: 5xx / timeout retry 1 time on same key
                for attempt in range(2):
                    try:
                        res = file_client.upload_file(
                            clean_env_id,
                            target_remote_path,
                            content,
                            overwrite=overwrite
                        )
                        if isinstance(res, dict) and res.get("error") == "CONFLICT":
                            failed_files.append(rel_p)
                            break
                        upload_ok = True
                        break
                    except requests.exceptions.HTTPError as he:
                        status = he.response.status_code if he.response is not None else 500
                        if status in (400, 401, 403, 404, 409):
                            break
                        if status in (500, 502, 503, 504) and attempt == 0:
                            continue
                        break
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                        if attempt == 0:
                            continue
                        break
                    except Exception:
                        break

                if not upload_ok:
                    failed_files.append(rel_p)
                    continue

                synced_files.append(rel_p)

                # 7. Integrity Verification via alt=media download & SHA256 comparison
                v_ok, v_err = verifier.verify_file(
                    clean_env_id,
                    rel_p,
                    expected_size=file_info["size"],
                    expected_sha256=file_info["sha256"],
                    base_dir="workspace"
                )
                if v_ok:
                    verified_files.append(rel_p)
                else:
                    failed_files.append(rel_p)

        # 8. Determine final SyncResult status
        if self.is_pool_mode and self.key_pool and key_ref:
            self.key_pool.report_result(key_ref, status_code=200)

        if failed_files:
            if synced_files:
                final_status = SyncStatus.SYNC_PARTIAL
                err_msg = f"Partial sync failure: {len(failed_files)} files failed."
            else:
                final_status = SyncStatus.TRANSFER_FAILED
                err_msg = f"Transfer failed for {len(failed_files)} files."
            return SyncResult(
                status=final_status,
                environment_id=clean_env_id,
                source_path=manifest.source_path,
                commit_sha=manifest.commit_sha,
                synced_files=synced_files,
                skipped_files=skipped_files,
                verified_files=verified_files,
                failed_files=failed_files,
                error_message=err_msg
            )

        return SyncResult(
            status=SyncStatus.SYNC_SUCCESS,
            environment_id=clean_env_id,
            source_path=manifest.source_path,
            commit_sha=manifest.commit_sha,
            synced_files=synced_files,
            skipped_files=skipped_files,
            verified_files=verified_files,
            failed_files=[],
            output=f"Successfully synced and verified {len(synced_files)} files."
        )

    def sync_to_remote_legacy_interaction(
        self,
        environment_id: str,
        manifest: SourceManifest,
        overwrite: bool = False
    ) -> SyncResult:
        """
        [DEPRECATED / LEGACY COMPATIBILITY]
        Interaction-based Base64 Python script injection synchronization.
        Preserved strictly for backward compatibility with legacy tests.
        """
        clean_env_id = EnvironmentFileClient.clean_environment_id(environment_id)

        for rel_path in manifest.files.keys():
            if not self._is_safe_rel_path(rel_path):
                return SyncResult(
                    status=SyncStatus.INVALID_PATH,
                    environment_id=clean_env_id,
                    error_message=f"INVALID_PATH: Path traversal attempt detected in path '{rel_path}'."
                )

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

        if self.is_pool_mode:
            client = AntigravityClient(
                key_pool=self.key_pool,
                project_id=self.project_id,
                registry=self.registry
            )
        else:
            api_key = get_api_key_by_index(self.key_index)
            if not api_key:
                return SyncResult(
                    status=SyncStatus.ERROR,
                    environment_id=clean_env_id,
                    error_message=f"No API key available for index {self.key_index}."
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
        [DEPRECATED / LEGACY COMPATIBILITY HELPER]
        Strictly parses structured integrity report from legacy agent interaction output.
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
