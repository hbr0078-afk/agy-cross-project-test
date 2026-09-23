import os
import mimetypes
import hashlib
import requests
import urllib.parse
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

DEFAULT_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB configurable chunk size

class SyncAction(Enum):
    UNCHANGED = "UNCHANGED"
    UPLOAD = "UPLOAD"
    UPDATE = "UPDATE"
    CONFLICT = "CONFLICT"
    REMOTE_ONLY = "REMOTE_ONLY"
    INVALID_PATH = "INVALID_PATH"
    ERROR = "ERROR"

@dataclass
class RemoteFileInfo:
    path: str
    type: str  # "FILE" or "DIRECTORY"
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    sha256: Optional[str] = None

@dataclass
class RemoteManifest:
    environment_id: str
    files: Dict[str, RemoteFileInfo] = field(default_factory=dict)  # rel_path (relative to workspace/) -> RemoteFileInfo

@dataclass
class PlannedFileAction:
    rel_path: str
    action: SyncAction
    local_size: Optional[int] = None
    local_sha256: Optional[str] = None
    remote_size: Optional[int] = None
    remote_sha256: Optional[str] = None
    reason: Optional[str] = None

@dataclass
class SyncPlan:
    environment_id: str
    actions: List[PlannedFileAction] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    has_conflicts: bool = False

def safe_encode_path(rel_path: str, base_dir: str = "workspace") -> str:
    """
    Safely joins base_dir and rel_path, validating against path traversal,
    and returns URL-encoded path suitable for Google Environment File API.
    """
    if not rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
        raise ValueError(f"Invalid path: '{rel_path}' cannot be empty or absolute.")
    if "\x00" in rel_path:
        raise ValueError(f"Invalid path: '{rel_path}' contains NUL byte.")

    # Normalize path
    norm = os.path.normpath(rel_path).replace("\\", "/")
    if norm == "." or norm == ".." or norm.startswith("../") or "/../" in norm or norm.endswith("/.."):
        raise ValueError(f"Path traversal detected in path: '{rel_path}'")

    full_path = f"{base_dir}/{norm}" if base_dir else norm
    # Safe encode with urllib.parse.quote, keeping '/' safe
    return urllib.parse.quote(full_path, safe="/")

class EnvironmentFileClient:
    """
    Client for Google Antigravity Environment File API.
    Provides direct listing, metadata inspection, upload, download, and pagination.
    """
    def __init__(self, api_key: str, timeout: int = 60, session: Optional[requests.Session] = None):
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"x-goog-api-key": self.api_key})
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/environments"
        self.upload_base_url = "https://generativelanguage.googleapis.com/upload/v1beta/environments"

    @staticmethod
    def clean_environment_id(env_id: str) -> str:
        if not env_id:
            return ""
        if env_id.startswith("environment-"):
            return env_id[len("environment-"):]
        return env_id

    def list_files(
        self,
        environment_id: str,
        path: str = "",
        recursive: bool = True,
        page_size: int = 100,
        page_token: Optional[str] = None
    ) -> Dict[str, Any]:
        clean_id = self.clean_environment_id(environment_id)
        url = f"{self.base_url}/{clean_id}/files"
        params: Dict[str, Any] = {
            "page_size": page_size,
            "recursive": str(recursive).lower()
        }
        if path:
            params["path"] = path
        if page_token:
            params["page_token"] = page_token

        res = self.session.get(url, params=params, timeout=self.timeout)
        if res.status_code == 404:
            raise FileNotFoundError(f"Environment or path not found: {res.text}")
        res.raise_for_status()
        return res.json()

    def list_all_files(self, environment_id: str, base_path: str = "workspace") -> List[RemoteFileInfo]:
        """
        Exhaustively lists all files in the given environment and base path,
        following all page tokens to completion.
        """
        clean_id = self.clean_environment_id(environment_id)
        files: List[RemoteFileInfo] = []
        page_token = None

        while True:
            data = self.list_files(
                clean_id,
                path=base_path,
                recursive=True,
                page_size=100,
                page_token=page_token
            )
            raw_files = data.get("files", [])
            for rf in raw_files:
                p = rf.get("path") or rf.get("name") or ""
                # Parse size safely
                size = None
                if "size_bytes" in rf:
                    try:
                        size = int(rf["size_bytes"])
                    except (ValueError, TypeError):
                        pass

                raw_type = (rf.get("type") or "FILE").upper()
                entry_type = "DIRECTORY" if raw_type in ("DIRECTORY", "DIR") else "FILE"

                files.append(RemoteFileInfo(
                    path=p,
                    type=entry_type,
                    size_bytes=size,
                    mime_type=rf.get("mime_type"),
                    created=rf.get("created"),
                    modified=rf.get("modified"),
                    sha256=rf.get("sha256") or rf.get("checksum")
                ))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return files

    def get_file_metadata(self, environment_id: str, path: str) -> RemoteFileInfo:
        clean_id = self.clean_environment_id(environment_id)
        encoded_path = safe_encode_path(path, base_dir="")
        url = f"{self.base_url}/{clean_id}/files/{encoded_path}"
        res = self.session.get(url, timeout=self.timeout)
        if res.status_code == 404:
            raise FileNotFoundError(f"File '{path}' not found on remote.")
        res.raise_for_status()
        rf = res.json()
        # In Google API, metadata for single file might be in {"files": [rf]} or direct rf
        if "files" in rf and isinstance(rf["files"], list) and rf["files"]:
            rf = rf["files"][0]

        size = None
        if "size_bytes" in rf:
            try:
                size = int(rf["size_bytes"])
            except (ValueError, TypeError):
                pass

        raw_type = (rf.get("type") or "FILE").upper()
        entry_type = "DIRECTORY" if raw_type in ("DIRECTORY", "DIR") else "FILE"

        return RemoteFileInfo(
            path=rf.get("path") or rf.get("name") or path,
            type=entry_type,
            size_bytes=size,
            mime_type=rf.get("mime_type"),
            created=rf.get("created"),
            modified=rf.get("modified"),
            sha256=rf.get("sha256") or rf.get("checksum")
        )

    def download_file(self, environment_id: str, path: str) -> bytes:
        clean_id = self.clean_environment_id(environment_id)
        encoded_path = safe_encode_path(path, base_dir="")
        url = f"{self.base_url}/{clean_id}/files/{encoded_path}"
        res = self.session.get(url, params={"alt": "media"}, timeout=self.timeout)
        if res.status_code == 404:
            raise FileNotFoundError(f"File '{path}' not found on remote.")
        res.raise_for_status()
        return res.content

    def upload_file(
        self,
        environment_id: str,
        path: str,
        content: bytes,
        mime_type: Optional[str] = None,
        overwrite: bool = False,
        chunk_size: int = DEFAULT_UPLOAD_CHUNK_SIZE
    ) -> Dict[str, Any]:
        clean_id = self.clean_environment_id(environment_id)
        encoded_path = safe_encode_path(path, base_dir="")
        init_url = f"{self.upload_base_url}/{clean_id}/files/{encoded_path}"
        
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(path)
        mime_type = mime_type or "application/octet-stream"

        total_length = len(content)
        init_params: Dict[str, str] = {"uploadType": "resumable"}
        if overwrite:
            init_params["overwrite"] = "true"

        init_headers: Dict[str, str] = {
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(total_length),
            "Content-Length": "0"
        }

        init_res = self.session.put(init_url, params=init_params, headers=init_headers, timeout=self.timeout)
        if init_res.status_code == 409:
            return {"error": "CONFLICT", "status_code": 409, "message": init_res.text}
        init_res.raise_for_status()

        upload_url = init_res.headers.get("Location")
        if not upload_url:
            # Fallback if direct response returned
            try:
                return init_res.json()
            except Exception:
                return {"status": "ok", "status_code": init_res.status_code}

        # Resumable chunk upload loop
        offset = 0
        while True:
            end = min(offset + chunk_size, total_length)
            chunk = content[offset:end]
            
            # Note: Do not attach x-goog-api-key to session for upload_url if requests session reuses headers,
            # but requests session headers are sent by default. Standard Google resumable upload URL accepts session.
            chunk_headers = {
                "Content-Type": mime_type,
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end - 1}/{total_length}" if total_length > 0 else "bytes */0"
            }

            res = self.session.put(upload_url, headers=chunk_headers, data=chunk, timeout=self.timeout)

            if res.status_code in (200, 201):
                try:
                    return res.json()
                except Exception:
                    return {"status": "ok", "status_code": res.status_code}
            elif res.status_code == 308:
                # Resume Incomplete
                range_header = res.headers.get("Range")
                if range_header and range_header.startswith("bytes=0-"):
                    offset = int(range_header.split("-")[1]) + 1
                else:
                    offset = end
            else:
                if res.status_code == 409:
                    return {"error": "CONFLICT", "status_code": 409, "message": res.text}
                res.raise_for_status()

class SyncPlanner:
    """
    Compares SourceManifest (local) vs RemoteManifest (remote)
    and computes concrete SyncPlan.
    """
    @staticmethod
    def build_remote_manifest(
        environment_id: str,
        remote_files: List[RemoteFileInfo],
        base_dir: str = "workspace"
    ) -> RemoteManifest:
        manifest = RemoteManifest(environment_id=environment_id)
        prefix = f"{base_dir}/" if base_dir else ""
        for rf in remote_files:
            if rf.type.upper() in ("DIRECTORY", "DIR"):
                continue
            norm_p = rf.path.replace("\\", "/")
            if norm_p.startswith("/"):
                norm_p = norm_p[1:]
            if prefix and norm_p.startswith(prefix):
                rel = norm_p[len(prefix):]
            else:
                rel = norm_p
            manifest.files[rel] = rf
        return manifest

    @classmethod
    def plan(
        cls,
        source_manifest: Any,  # SourceManifest
        remote_manifest: RemoteManifest,
        file_client: Optional[EnvironmentFileClient] = None,
        overwrite: bool = False
    ) -> SyncPlan:
        plan = SyncPlan(environment_id=remote_manifest.environment_id)
        remote_unmatched = set(remote_manifest.files.keys())

        for rel_path, local_file in source_manifest.files.items():
            norm_rel = os.path.normpath(rel_path).replace("\\", "/")
            local_size = local_file["size"]
            local_sha = local_file["sha256"]

            if norm_rel not in remote_manifest.files:
                # File does not exist on remote -> UPLOAD
                plan.actions.append(PlannedFileAction(
                    rel_path=norm_rel,
                    action=SyncAction.UPLOAD,
                    local_size=local_size,
                    local_sha256=local_sha,
                    reason="New file on remote"
                ))
            else:
                remote_unmatched.discard(norm_rel)
                remote_info = remote_manifest.files[norm_rel]
                remote_sha = remote_info.sha256
                remote_size = remote_info.size_bytes

                # Determine if content matches
                content_matches = False
                if remote_sha:
                    content_matches = (remote_sha.lower() == local_sha.lower())
                elif remote_size is not None and remote_size != local_size:
                    content_matches = False
                elif file_client is not None:
                    # Remote metadata does not have SHA256, download and check actual SHA256
                    try:
                        remote_bytes = file_client.download_file(
                            remote_manifest.environment_id,
                            f"workspace/{norm_rel}"
                        )
                        calculated_sha = hashlib.sha256(remote_bytes).hexdigest()
                        remote_sha = calculated_sha
                        content_matches = (calculated_sha.lower() == local_sha.lower())
                    except Exception:
                        content_matches = False

                if content_matches:
                    plan.actions.append(PlannedFileAction(
                        rel_path=norm_rel,
                        action=SyncAction.UNCHANGED,
                        local_size=local_size,
                        local_sha256=local_sha,
                        remote_size=remote_size,
                        remote_sha256=remote_sha,
                        reason="Content identical"
                    ))
                else:
                    if overwrite:
                        plan.actions.append(PlannedFileAction(
                            rel_path=norm_rel,
                            action=SyncAction.UPDATE,
                            local_size=local_size,
                            local_sha256=local_sha,
                            remote_size=remote_size,
                            remote_sha256=remote_sha,
                            reason="Overwrite existing different file"
                        ))
                    else:
                        plan.actions.append(PlannedFileAction(
                            rel_path=norm_rel,
                            action=SyncAction.CONFLICT,
                            local_size=local_size,
                            local_sha256=local_sha,
                            remote_size=remote_size,
                            remote_sha256=remote_sha,
                            reason="Remote file exists with different content and overwrite=False"
                        ))
                        plan.conflicts.append(norm_rel)

        for rem_path in remote_unmatched:
            plan.actions.append(PlannedFileAction(
                rel_path=rem_path,
                action=SyncAction.REMOTE_ONLY,
                remote_size=remote_manifest.files[rem_path].size_bytes,
                remote_sha256=remote_manifest.files[rem_path].sha256,
                reason="File exists only on remote"
            ))

        plan.has_conflicts = len(plan.conflicts) > 0
        return plan

class IntegrityVerifier:
    """
    Verifies uploaded files by downloading remote bytes via alt=media
    and comparing actual SHA-256 and byte sizes with local source.
    """
    def __init__(self, file_client: EnvironmentFileClient):
        self.file_client = file_client

    def verify_file(
        self,
        environment_id: str,
        rel_path: str,
        expected_size: int,
        expected_sha256: str,
        base_dir: str = "workspace"
    ) -> Tuple[bool, Optional[str]]:
        remote_path = f"{base_dir}/{rel_path}" if base_dir else rel_path
        try:
            content = self.file_client.download_file(environment_id, remote_path)
            actual_size = len(content)
            actual_sha256 = hashlib.sha256(content).hexdigest()

            if actual_size != expected_size:
                return False, f"Size mismatch for '{rel_path}': expected {expected_size}, got {actual_size}"
            if actual_sha256.lower() != expected_sha256.lower():
                return False, f"SHA256 mismatch for '{rel_path}': expected {expected_sha256}, got {actual_sha256}"
            return True, None
        except Exception as e:
            return False, f"Failed to download and verify '{rel_path}': {e}"
