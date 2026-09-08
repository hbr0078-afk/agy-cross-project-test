import os
import re
import subprocess
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

class GitState(Enum):
    CLEAN = "CLEAN"
    CHANGES_DETECTED = "CHANGES_DETECTED"
    UNTRACKED_FILES = "UNTRACKED_FILES"
    CONFLICT = "CONFLICT"
    INVALID_REPOSITORY = "INVALID_REPOSITORY"
    ERROR = "ERROR"

class GitOpStatus(Enum):
    SUCCESS = "SUCCESS"
    NOTHING_TO_COMMIT = "NOTHING_TO_COMMIT"
    COMMITTED = "COMMITTED"
    PUSHED = "PUSHED"
    PUSH_FAILED = "PUSH_FAILED"
    PULL_SUCCESS = "PULL_SUCCESS"
    PULL_FAILED = "PULL_FAILED"
    CONFLICT = "CONFLICT"
    AUTH_FAILED = "AUTH_FAILED"
    INVALID_REPOSITORY = "INVALID_REPOSITORY"
    INVALID_BRANCH = "INVALID_BRANCH"
    ERROR = "ERROR"

@dataclass
class GitStatusResult:
    state: GitState
    branch: str = ""
    head_commit: str = ""
    remote_origin: str = ""
    staged_files: List[str] = field(default_factory=list)
    unstaged_files: List[str] = field(default_factory=list)
    untracked_files: List[str] = field(default_factory=list)
    conflict_files: List[str] = field(default_factory=list)
    has_changes: bool = False
    raw_status: str = ""
    error_message: Optional[str] = None

@dataclass
class GitOperationResult:
    status: GitOpStatus
    commit_sha: Optional[str] = None
    commit_message: Optional[str] = None
    files_affected: List[str] = field(default_factory=list)
    output: str = ""
    error_message: Optional[str] = None

def mask_credentials(text: str) -> str:
    """Mask tokens, passwords, and API keys from logs/outputs."""
    if not text:
        return ""
    masked = re.sub(r'(https?://[^:\s]+:)([^@\s]+)(@)', r'\1***\3', text)
    masked = re.sub(r'(gh[pousr]_[A-Za-z0-9_]{20,})', r'***', masked)
    masked = re.sub(r'(github_pat_[A-Za-z0-9_]{20,})', r'***', masked)
    masked = re.sub(r'(AIza[A-Za-z0-9_\-]{30,})', r'***', masked)
    return masked

class GitManager:
    def __init__(self, repo_path: str):
        self.repo_path = os.path.abspath(os.path.expanduser(repo_path))

    def _run_git(self, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
        """Executes a git command in repo_path and captures output safely."""
        cmd = ["git", "-C", self.repo_path] + args
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check
            )
            return res
        except Exception as e:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=128,
                stdout="",
                stderr=str(e)
            )

    def is_valid_repository(self) -> bool:
        if not os.path.isdir(self.repo_path):
            return False
        res = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return res.returncode == 0 and res.stdout.strip() == "true"

    def get_status(self) -> GitStatusResult:
        if not self.is_valid_repository():
            return GitStatusResult(
                state=GitState.INVALID_REPOSITORY,
                error_message=f"Path '{self.repo_path}' is not a valid git repository."
            )

        # 1. Get branch
        b_res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = b_res.stdout.strip() if b_res.returncode == 0 else "UNKNOWN"

        # 2. Get HEAD commit
        h_res = self._run_git(["rev-parse", "HEAD"])
        head_commit = h_res.stdout.strip() if h_res.returncode == 0 else ""

        # 3. Get remote origin
        r_res = self._run_git(["remote", "get-url", "origin"])
        remote_origin = mask_credentials(r_res.stdout.strip()) if r_res.returncode == 0 else ""

        # 4. Check for merge head or unmerged paths
        merge_head_path = os.path.join(self.repo_path, ".git", "MERGE_HEAD")
        has_merge_head = os.path.exists(merge_head_path)

        # 5. Get porcelain status
        s_res = self._run_git(["status", "--porcelain"])
        if s_res.returncode != 0:
            return GitStatusResult(
                state=GitState.ERROR,
                branch=branch,
                head_commit=head_commit,
                remote_origin=remote_origin,
                error_message=mask_credentials(s_res.stderr.strip())
            )

        staged, unstaged, untracked, conflicts = [], [], [], []

        for line in s_res.stdout.splitlines():
            if not line:
                continue
            idx_status = line[0]
            work_status = line[1]
            filepath = line[3:].strip()

            # Unmerged/conflict states in git status --porcelain:
            # DD, AU, UD, UA, DU, AA, UU
            if (idx_status, work_status) in [('U','U'), ('A','A'), ('D','D'), ('A','U'), ('U','D'), ('U','A'), ('D','U')]:
                conflicts.append(filepath)
            elif idx_status in ['M', 'A', 'D', 'R', 'C']:
                staged.append(filepath)
            elif work_status in ['M', 'D']:
                unstaged.append(filepath)
            elif idx_status == '?' and work_status == '?':
                untracked.append(filepath)

        is_conflict = bool(conflicts) or has_merge_head
        has_changes = bool(staged or unstaged or untracked or conflicts)

        if is_conflict:
            state = GitState.CONFLICT
        elif has_changes:
            state = GitState.CHANGES_DETECTED
        else:
            state = GitState.CLEAN

        return GitStatusResult(
            state=state,
            branch=branch,
            head_commit=head_commit,
            remote_origin=remote_origin,
            staged_files=staged,
            unstaged_files=unstaged,
            untracked_files=untracked,
            conflict_files=conflicts,
            has_changes=has_changes,
            raw_status=mask_credentials(s_res.stdout)
        )

    def get_diff(self, staged: bool = False, file_path: Optional[str] = None) -> str:
        if not self.is_valid_repository():
            return ""
        args = ["diff"]
        if staged:
            args.append("--staged")
        if file_path:
            args.extend(["--", file_path])
        res = self._run_git(args)
        return mask_credentials(res.stdout)

    def commit_checkpoint(self, message: str, files: Optional[List[str]] = None) -> GitOperationResult:
        status = self.get_status()
        if status.state == GitState.INVALID_REPOSITORY:
            return GitOperationResult(
                status=GitOpStatus.INVALID_REPOSITORY,
                error_message=status.error_message
            )
        if status.state == GitState.CONFLICT:
            return GitOperationResult(
                status=GitOpStatus.CONFLICT,
                error_message="Cannot create checkpoint commit: merge conflicts exist in repository."
            )
        if not status.has_changes:
            return GitOperationResult(
                status=GitOpStatus.NOTHING_TO_COMMIT,
                commit_sha=status.head_commit,
                output="Working tree is clean. Nothing to commit."
            )

        if files:
            add_args = ["add", "--"] + files
            affected = files
        else:
            add_args = ["add", "-A"]
            affected = status.staged_files + status.unstaged_files + status.untracked_files

        add_res = self._run_git(add_args)
        if add_res.returncode != 0:
            return GitOperationResult(
                status=GitOpStatus.ERROR,
                error_message=f"Failed to stage files: {mask_credentials(add_res.stderr.strip())}"
            )

        c_res = self._run_git(["commit", "-m", message])
        if c_res.returncode != 0:
            if "nothing to commit" in c_res.stdout.lower() or "nothing to commit" in c_res.stderr.lower():
                return GitOperationResult(
                    status=GitOpStatus.NOTHING_TO_COMMIT,
                    commit_sha=status.head_commit,
                    output=mask_credentials(c_res.stdout)
                )
            return GitOperationResult(
                status=GitOpStatus.ERROR,
                error_message=mask_credentials(c_res.stderr.strip() or c_res.stdout.strip())
            )

        new_head_res = self._run_git(["rev-parse", "HEAD"])
        new_commit_sha = new_head_res.stdout.strip() if new_head_res.returncode == 0 else ""

        return GitOperationResult(
            status=GitOpStatus.COMMITTED,
            commit_sha=new_commit_sha,
            commit_message=message,
            files_affected=affected,
            output=mask_credentials(c_res.stdout.strip())
        )

    def push(self, remote: str = "origin", branch: Optional[str] = None) -> GitOperationResult:
        if not self.is_valid_repository():
            return GitOperationResult(
                status=GitOpStatus.INVALID_REPOSITORY,
                error_message="Invalid git repository."
            )

        status = self.get_status()
        target_branch = branch or status.branch
        if not target_branch or target_branch == "HEAD":
            return GitOperationResult(
                status=GitOpStatus.INVALID_BRANCH,
                error_message=f"Cannot push detached or invalid branch '{target_branch}'."
            )

        res = self._run_git(["push", remote, target_branch])
        masked_err = mask_credentials(res.stderr.strip())
        masked_out = mask_credentials(res.stdout.strip())

        if res.returncode != 0:
            if "authentication failed" in masked_err.lower() or "permission to" in masked_err.lower() or "403" in masked_err:
                return GitOperationResult(
                    status=GitOpStatus.AUTH_FAILED,
                    error_message=f"GitHub authentication/permission failed: {masked_err}"
                )
            return GitOperationResult(
                status=GitOpStatus.PUSH_FAILED,
                error_message=f"git push failed: {masked_err}"
            )

        return GitOperationResult(
            status=GitOpStatus.PUSHED,
            commit_sha=status.head_commit,
            output=masked_out or masked_err
        )

    def pull(self, remote: str = "origin", branch: Optional[str] = None, no_rebase: bool = True) -> GitOperationResult:
        if not self.is_valid_repository():
            return GitOperationResult(
                status=GitOpStatus.INVALID_REPOSITORY,
                error_message="Invalid git repository."
            )

        status = self.get_status()
        target_branch = branch or status.branch
        
        args = ["pull"]
        if no_rebase:
            args.append("--no-rebase")
        args.extend([remote, target_branch])

        res = self._run_git(args)
        masked_err = mask_credentials(res.stderr.strip())
        masked_out = mask_credentials(res.stdout.strip())

        if res.returncode != 0:
            combined = f"{masked_out}\n{masked_err}".lower()
            if "conflict" in combined or "divergent branches" in combined:
                return GitOperationResult(
                    status=GitOpStatus.CONFLICT,
                    error_message=f"Merge conflict or divergent branches during git pull: {masked_out or masked_err}"
                )
            if "authentication failed" in combined or "permission to" in combined or "403" in combined:
                return GitOperationResult(
                    status=GitOpStatus.AUTH_FAILED,
                    error_message=f"Authentication failed during git pull: {masked_err}"
                )
            return GitOperationResult(
                status=GitOpStatus.PULL_FAILED,
                error_message=f"git pull failed: {masked_err}"
            )

        new_head_res = self._run_git(["rev-parse", "HEAD"])
        new_commit = new_head_res.stdout.strip() if new_head_res.returncode == 0 else ""

        return GitOperationResult(
            status=GitOpStatus.PULL_SUCCESS,
            commit_sha=new_commit,
            output=masked_out
        )
