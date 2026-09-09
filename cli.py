#!/usr/bin/env python3
import sys
import os
import argparse
from keys import get_api_key_by_index
from client import AntigravityClient
from registry import ProjectRegistry, RegistryCorruptedError
from git_manager import GitManager, GitState, GitOpStatus
from snapshot_manager import SnapshotManager

def cmd_test_agent(args):
    api_key = ***)
    if not api_key:
        ***"Error: No API key found for index {args.key_index}.", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Found API key reference for Key {args.key_index} (length: {len(api_key)}, value masked).")
    print(f"[*] Initializing AntigravityClient with agent 'antigravity-preview-05-2026'...")
    client = AntigravityClient(api_key=***

    prompt = args.prompt or "Hello, please confirm you are ready by replying with exactly: ANTIGRAVITY_AGENT_ONLINE"
    print(f"[*] Sending prompt to remote environment: {prompt!r}")

    result = client.create_interaction(
        prompt=prompt,
        environment="remote",
        timeout=120
    )

    if not result.get("success"):
        print(f"[!] Request Failed: HTTP {result.get('status_code')}", file=sys.stderr)
        print(f"[!] Error Details: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    print("\n[+] Interaction Successful!")
    print(f"    - Environment ID: {result.get('environment_id')}")
    print(f"    - Interaction ID: {result.get('interaction_id')}")
    print(f"    - Status:         {result.get('status')}")
    if result.get("usage"):
        usage = result["usage"]
        print(f"    - Total Tokens:   {usage.get('total_tokens')}")
    print("\n=== Agent Output ===")
    print(result.get("output") or "(No text output)")
    print("====================")

def cmd_project_register(args):
    registry = ProjectRegistry()
    try:
        entry = registry.register_project(args.path, args.project_id)
        print(f"[+] Project successfully registered:")
        print(f"    - Project ID:     {entry['project_id']}")
        print(f"    - Local Path:     {entry['path']}")
        print(f"    - GitHub Repo:    {entry['repo'] or '(none)'}")
        print(f"    - Branch:         {entry['branch']}")
        print(f"    - Active Key Ref: {entry['active_key']}")
        print(f"    - State:          {entry['state']}")
    except RegistryCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[!] Failed to register project: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_project_status(args):
    registry = ProjectRegistry()
    try:
        entry = None
        if args.project_id:
            entry = registry.get_project(args.project_id)
            if not entry:
                print(f"[!] Project '{args.project_id}' not found in registry.", file=sys.stderr)
                sys.exit(1)
        else:
            cwd = os.getcwd()
            entry = registry.find_project_by_path(cwd)
            if not entry:
                print(f"[!] No registered project found for current directory '{cwd}'.", file=sys.stderr)
                print(f"    Run 'agy-router project register .' or specify --project-id <id>.", file=sys.stderr)
                sys.exit(1)

        print("=== Project Status ===")
        print(f"Project ID:          {entry.get('project_id')}")
        print(f"Local Path:          {entry.get('path')}")
        print(f"GitHub Repo:         {entry.get('repo') or '(none)'}")
        print(f"Branch:              {entry.get('branch')}")
        print(f"Active Key Ref:      {entry.get('active_key')}")
        print(f"Environment ID:      {entry.get('environment_id') or '(none)'}")
        print(f"Last Interaction ID: {entry.get('last_interaction_id') or '(none)'}")
        print(f"Last Commit:         {entry.get('last_commit') or '(none)'}")
        print(f"State:               {entry.get('state')}")
        print(f"Roadmap File:        {entry.get('roadmap')}")
        print("======================")
    except RegistryCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_project_list(args):
    registry = ProjectRegistry()
    try:
        projects = registry.list_projects()
        if not projects:
            print("No projects registered yet.")
            return

        print(f"{'PROJECT ID':<25} {'ACTIVE KEY':<12} {'STATE':<10} {'PATH'}")
        print("-" * 75)
        for p in projects:
            print(f"{p.get('project_id', ''):<25} {p.get('active_key', ''):<12} {p.get('state', ''):<10} {p.get('path', '')}")
    except RegistryCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)

def _get_target_repo_path(args) -> str:
    if getattr(args, "path", None):
        return os.path.abspath(args.path)
    registry = ProjectRegistry()
    if getattr(args, "project_id", None):
        p = registry.get_project(args.project_id)
        if p and p.get("path"):
            return p["path"]
    cwd = os.getcwd()
    p = registry.find_project_by_path(cwd)
    if p and p.get("path"):
        return p["path"]
    return cwd

def cmd_git_status(args):
    repo_path = _get_target_repo_path(args)
    gm = GitManager(repo_path)
    res = gm.get_status()
    if res.state == GitState.INVALID_REPOSITORY:
        print(f"[!] {res.error_message}", file=sys.stderr)
        sys.exit(1)

    print(f"=== Git Status ({repo_path}) ===")
    print(f"State:        {res.state.value}")
    print(f"Branch:       {res.branch}")
    print(f"HEAD Commit:  {res.head_commit[:7] if res.head_commit else '(none)'}")
    print(f"Remote:       {res.remote_origin or '(none)'}")
    print(f"Has Changes:  {res.has_changes}")
    if res.staged_files:
        print(f"Staged:       {', '.join(res.staged_files)}")
    if res.unstaged_files:
        print(f"Unstaged:     {', '.join(res.unstaged_files)}")
    if res.untracked_files:
        print(f"Untracked:    {', '.join(res.untracked_files)}")
    print("================================")

def cmd_git_diff(args):
    repo_path = _get_target_repo_path(args)
    gm = GitManager(repo_path)
    diff_out = gm.get_diff(staged=args.staged)
    if not diff_out:
        print("(No diff)")
    else:
        print(diff_out)

def cmd_git_checkpoint(args):
    repo_path = _get_target_repo_path(args)
    gm = GitManager(repo_path)
    msg = args.message or "checkpoint: automated save"
    res = gm.commit_checkpoint(msg)
    if res.status == GitOpStatus.NOTHING_TO_COMMIT:
        print(f"[*] Nothing to commit: working tree is clean (HEAD: {res.commit_sha[:7] if res.commit_sha else 'unknown'}).")
    elif res.status == GitOpStatus.COMMITTED:
        print(f"[+] Checkpoint commit created:")
        print(f"    - Commit SHA: {res.commit_sha[:7] if res.commit_sha else 'unknown'}")
        print(f"    - Message:    {res.commit_message}")
        print(f"    - Files:      {', '.join(res.files_affected)}")
        try:
            registry = ProjectRegistry()
            p = registry.find_project_by_path(repo_path)
            if p:
                registry.update_project_state(p["project_id"], last_commit=res.commit_sha)
        except Exception:
            pass
    else:
        print(f"[!] Checkpoint failed ({res.status.value}): {res.error_message}", file=sys.stderr)
        sys.exit(1)

def cmd_git_push(args):
    repo_path = _get_target_repo_path(args)
    gm = GitManager(repo_path)
    res = gm.push(remote=args.remote, branch=args.branch)
    if res.status == GitOpStatus.PUSHED:
        print(f"[+] Git push successful to {args.remote} (HEAD: {res.commit_sha[:7] if res.commit_sha else 'unknown'})")
    else:
        print(f"[!] Git push failed ({res.status.value}): {res.error_message}", file=sys.stderr)
        sys.exit(1)

def cmd_git_pull(args):
    repo_path = _get_target_repo_path(args)
    gm = GitManager(repo_path)
    res = gm.pull(remote=args.remote, branch=args.branch)
    if res.status == GitOpStatus.PULL_SUCCESS:
        print(f"[+] Git pull successful from {args.remote} (HEAD: {res.commit_sha[:7] if res.commit_sha else 'unknown'})")
    else:
        print(f"[!] Git pull failed ({res.status.value}): {res.error_message}", file=sys.stderr)
        sys.exit(1)

def cmd_snapshot_download(args):
    sm = SnapshotManager(key_index=args.key_index)
    dest = args.destination or f"environment-{args.environment_id}.tar"
    print(f"[*] Downloading snapshot for environment-{args.environment_id}...")
    res = sm.download_snapshot(args.environment_id, dest)
    if res.success:
        print(f"[+] Snapshot downloaded successfully:")
        print(f"    - Saved to:       {res.snapshot_path}")
        print(f"    - Environment ID: {res.environment_id}")
        print(f"    - Size (bytes):   {res.size_bytes}")
        print(f"    - SHA256:         {res.sha256}")
    else:
        print(f"[!] Snapshot download failed: {res.error_message}", file=sys.stderr)
        sys.exit(1)

def cmd_snapshot_inspect(args):
    sm = SnapshotManager()
    insp = sm.inspect_snapshot(args.snapshot)
    if insp.is_valid:
        print(f"=== Snapshot Metadata ===")
        print(f"Path:            {insp.snapshot_path}")
        print(f"Size (bytes):    {insp.size_bytes}")
        print(f"SHA256:          {insp.sha256}")
        print(f"File Count:      {insp.file_count}")
        print(f"Has Workspace:   {insp.has_workspace}")
        print(f"Workspace Files: {len(insp.workspace_files)}")
        print("\n--- File List ---")
        for f in insp.file_list:
            print(f"  {f}")
        print("=========================")
    else:
        print(f"[!] Snapshot inspection failed: {insp.error_message}", file=sys.stderr)
        sys.exit(1)

def cmd_snapshot_restore(args):
    sm = SnapshotManager()
    print(f"[*] Restoring snapshot '{args.snapshot}' to '{args.destination}'...")
    res = sm.restore_snapshot(args.snapshot, args.destination)
    if res.success:
        print(f"[+] Snapshot restored successfully:")
        print(f"    - Destination:     {res.destination}")
        print(f"    - Files Extracted: {len(res.files_extracted)}")
    else:
        print(f"[!] Snapshot restore failed: {res.error_message}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(prog="agy-router", description="Antigravity Multi-Key Router")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # test-agent
    test_parser = subparsers.add_parser("test-agent", help="Test Antigravity Agent connection")
    test_parser.add_argument("--key-index", type=int, default=1, help="1-based API key index (default: 1)")
    test_parser.add_argument("--prompt", type=str, default=None, help="Custom test prompt")
    test_parser.set_defaults(func=cmd_test_agent)

    # project
    project_parser = subparsers.add_parser("project", help="Manage projects and persistent sessions")
    project_sub = project_parser.add_subparsers(dest="project_cmd", help="Project commands")
    reg_parser = project_sub.add_parser("register", help="Register a directory as a project")
    reg_parser.add_argument("path", type=str, help="Path to project directory")
    reg_parser.add_argument("--project-id", type=str, default=None, help="Custom project ID")
    reg_parser.set_defaults(func=cmd_project_register)
    status_parser = project_sub.add_parser("status", help="Show project status")
    status_parser.add_argument("--project-id", type=str, default=None, help="Project ID")
    status_parser.set_defaults(func=cmd_project_status)
    list_parser = project_sub.add_parser("list", help="List all registered projects")
    list_parser.set_defaults(func=cmd_project_list)

    # git
    git_parser = subparsers.add_parser("git", help="Manage Git status, checkpoints, and push/pull")
    git_sub = git_parser.add_subparsers(dest="git_cmd", help="Git subcommands")
    g_status = git_sub.add_parser("status", help="Get repository git status")
    g_status.add_argument("--path", type=str, default=None, help="Repo path")
    g_status.add_argument("--project-id", type=str, default=None, help="Project ID")
    g_status.set_defaults(func=cmd_git_status)

    g_diff = git_sub.add_parser("diff", help="Get working tree diff")
    g_diff.add_argument("--staged", action="store_true", help="View staged diff")
    g_diff.add_argument("--path", type=str, default=None, help="Repo path")
    g_diff.set_defaults(func=cmd_git_diff)

    g_cp = git_sub.add_parser("checkpoint", help="Safely commit checkpoint if changes exist")
    g_cp.add_argument("-m", "--message", type=str, default=None, help="Checkpoint message")
    g_cp.add_argument("--path", type=str, default=None, help="Repo path")
    g_cp.set_defaults(func=cmd_git_checkpoint)

    g_push = git_sub.add_parser("push", help="Push commits to remote")
    g_push.add_argument("--remote", type=str, default="origin", help="Remote name (default: origin)")
    g_push.add_argument("--branch", type=str, default=None, help="Branch name")
    g_push.add_argument("--path", type=str, default=None, help="Repo path")
    g_push.set_defaults(func=cmd_git_push)

    g_pull = git_sub.add_parser("pull", help="Pull commits from remote")
    g_pull.add_argument("--remote", type=str, default="origin", help="Remote name (default: origin)")
    g_pull.add_argument("--branch", type=str, default=None, help="Branch name")
    g_pull.add_argument("--path", type=str, default=None, help="Repo path")
    g_pull.set_defaults(func=cmd_git_pull)

    # snapshot
    snap_parser = subparsers.add_parser("snapshot", help="Download, inspect, and restore environment snapshots")
    snap_sub = snap_parser.add_subparsers(dest="snapshot_cmd", help="Snapshot commands")
    
    s_dl = snap_sub.add_parser("download", help="Download remote environment snapshot tar archive")
    s_dl.add_argument("environment_id", type=str, help="Environment ID")
    s_dl.add_argument("destination", type=str, nargs="?", default=None, help="Destination file or directory path")
    s_dl.add_argument("--key-index", type=int, default=1, help="API key index (default: 1)")
    s_dl.set_defaults(func=cmd_snapshot_download)

    s_insp = snap_sub.add_parser("inspect", help="Inspect snapshot metadata and file contents")
    s_insp.add_argument("snapshot", type=str, help="Path to snapshot tar archive")
    s_insp.set_defaults(func=cmd_snapshot_inspect)

    s_rst = snap_sub.add_parser("restore", help="Safely restore snapshot to destination directory")
    s_rst.add_argument("snapshot", type=str, help="Path to snapshot tar archive")
    s_rst.add_argument("destination", type=str, help="Destination directory path")
    s_rst.set_defaults(func=cmd_snapshot_restore)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)

if __name__ == "__main__":
    main()
