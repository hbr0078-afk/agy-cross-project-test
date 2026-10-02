#!/usr/bin/env python3
import sys
import os
import argparse
from typing import Optional, List, Dict, Any
from keys import get_api_key_by_index, KeyPoolManager, AllKeysExhaustedError
from client import AntigravityClient
from registry import ProjectRegistry, RegistryCorruptedError
from sessions import SessionStateManager, SessionCorruptedError
from git_manager import GitManager, GitState, GitOpStatus
from snapshot_manager import SnapshotManager
from workspace_sync import WorkspaceSync
from config import RouterConfig, load_config, save_config, get_config_path, ConfigCorruptedError


def cmd_test_agent(args):
    api_key = get_api_key_by_index(args.key_index)
    if not api_key:
        print(f"Error: No API key found for index {args.key_index}.", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Found API key reference for Key {args.key_index} (length: {len(api_key)}, value masked).")
    print(f"[*] Initializing AntigravityClient with agent 'antigravity-preview-05-2026'...")
    client = AntigravityClient(api_key=api_key)

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
        print(f"    - Project ID:  {entry['project_id']}")
        print(f"    - Local Path:  {entry['path']}")
        print(f"    - GitHub Repo: {entry['repo'] or '(none)'}")
        print(f"    - Branch:      {entry['branch']}")
        print(f"    - State:       {entry['state']}")
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

        # Project metadata ONLY - no runtime fields (active_key, environment_id, last_interaction_id)
        print("=== Project Status ===")
        print(f"Project ID:     {entry.get('project_id')}")
        print(f"Local Path:     {entry.get('path')}")
        print(f"GitHub Repo:    {entry.get('repo') or '(none)'}")
        print(f"Branch:         {entry.get('branch')}")
        print(f"Last Commit:    {entry.get('last_commit') or '(none)'}")
        print(f"State:          {entry.get('state')}")
        print(f"Roadmap File:   {entry.get('roadmap')}")
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

        # Project metadata ONLY - no runtime fields
        print(f"{'PROJECT ID':<25} {'STATE':<10} {'PATH'}")
        print("-" * 75)
        for p in projects:
            print(f"{p.get('project_id', ''):<25} {p.get('state', ''):<10} {p.get('path', '')}")
    except RegistryCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_session_create(args):
    sess_mgr = SessionStateManager()
    try:
        entry = sess_mgr.create_session(
            project_id=args.project_id,
            session_id=args.session_id,
            bound_key=args.bound_key,
            environment_id=args.environment_id,
            tenant_id=args.tenant_id
        )
        print(f"[+] Session created:")
        print(f"    - Session ID:         {entry['session_id']}")
        print(f"    - Project ID:         {entry['project_id']}")
        print(f"    - Bound Key:          {entry['bound_key']}")
        print(f"    - Environment ID:     {entry['environment_id'] or '(none)'}")
        print(f"    - Tenant ID:          {entry['tenant_id'] or '(none)'}")
        print(f"    - State:              {entry['state']}")
    except Exception as e:
        print(f"[!] Failed to create session: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_session_list(args):
    sess_mgr = SessionStateManager()
    try:
        sessions = sess_mgr.list_sessions(project_id=args.project_id)
        if not sessions:
            print("No sessions found.")
            return

        print(f"{'SESSION ID':<35} {'PROJECT ID':<25} {'BOUND KEY':<10} {'TENANT':<12} {'ENVIRONMENT':<20} {'STATE':<10}")
        print("-" * 112)
        for s in sessions:
            print(f"{s.get('session_id', ''):<35} {s.get('project_id', ''):<25} {s.get('bound_key', ''):<10} {s.get('tenant_id', ''):<12} {s.get('environment_id', ''):<20} {s.get('state', ''):<10}")
    except SessionCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_session_status(args):
    sess_mgr = SessionStateManager()
    try:
        entry = sess_mgr.get_session(args.session_id)
        if not entry:
            print(f"[!] Session '{args.session_id}' not found.", file=sys.stderr)
            sys.exit(1)

        print("=== Session Status ===")
        print(f"Session ID:         {entry.get('session_id')}")
        print(f"Project ID:         {entry.get('project_id')}")
        print(f"Bound Key:          {entry.get('bound_key')}")
        print(f"Tenant ID:          {entry.get('tenant_id') or '(none)'}")
        print(f"Environment ID:     {entry.get('environment_id') or '(none)'}")
        print(f"Last Interaction:   {entry.get('last_interaction_id') or '(none)'}")
        print(f"State:              {entry.get('state')}")
        print(f"Created At:         {entry.get('created_at')}")
        print(f"Updated At:         {entry.get('updated_at')}")
        print("======================")
    except SessionCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_session_update(args):
    sess_mgr = SessionStateManager()
    try:
        entry = sess_mgr.update_session(
            session_id=args.session_id,
            bound_key=args.bound_key,
            environment_id=args.environment_id,
            last_interaction_id=args.last_interaction_id,
            tenant_id=args.tenant_id,
            state=args.state
        )
        print(f"[+] Session updated:")
        print(f"    - Session ID:         {entry['session_id']}")
        print(f"    - Project ID:         {entry['project_id']}")
        print(f"    - Bound Key:          {entry['bound_key']}")
        print(f"    - Environment ID:     {entry['environment_id'] or '(none)'}")
        print(f"    - Tenant ID:          {entry['tenant_id'] or '(none)'}")
        print(f"    - Last Interaction:   {entry['last_interaction_id'] or '(none)'}")
        print(f"    - State:              {entry['state']}")
    except SessionCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"[!] Session not found: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"[!] Invalid value: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_session_invalidate(args):
    sess_mgr = SessionStateManager()
    try:
        entry = sess_mgr.invalidate_session(args.session_id)
        print(f"[+] Session invalidated:")
        print(f"    - Session ID: {entry['session_id']}")
        print(f"    - State:      {entry['state']}")
    except SessionCorruptedError as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"[!] Session not found: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_keypool_list(args):
    pool = KeyPoolManager()
    try:
        status = pool.get_status()
        if not status:
            print("No keys discovered.")
            return

        print(f"{'KEY REF':<10} {'INDEX':<6} {'STATE':<10} {'TENANT':<12} {'FAIL COUNT':<10} {'LAST USED':<20}")
        print("-" * 78)
        for ref, entry in sorted(status.items()):
            last_used = entry.get('last_used_at', 0)
            last_used_str = ""
            if last_used:
                import time
                last_used_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_used))
            print(f"{ref:<10} {entry.get('index', ''):<6} {entry.get('state', ''):<10} {entry.get('tenant_id', ''):<12} {entry.get('fail_count', 0):<10} {last_used_str:<20}")
    except Exception as e:
        print(f"[!] Failed to list keys: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_keypool_status(args):
    pool = KeyPoolManager()
    try:
        status = pool.get_status()
        if not status:
            print("No keys discovered.")
            return

        print("=== Key Pool Status ===")
        for ref, entry in sorted(status.items()):
            print(f"Key: {ref} (index={entry.get('index')})")
            print(f"  State:         {entry.get('state')}")
            print(f"  Tenant:        {entry.get('tenant_id') or '(none)'}")
            print(f"  Fail Count:    {entry.get('fail_count', 0)}")
            print(f"  Last Status:   {entry.get('last_status_code') or '(none)'}")
            print(f"  Cooldown Until:{entry.get('cooldown_until') or '(none)'}")
            print(f"  Last Used:     {entry.get('last_used_at', 0)}")
            print(f"  Last Success:  {entry.get('last_success_at', 0)}")
            print()
    except Exception as e:
        print(f"[!] Failed to get key pool status: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_keypool_reset(args):
    pool = KeyPoolManager()
    try:
        success = pool.reset_key(args.key_ref)
        if success:
            print(f"[+] Key '{args.key_ref}' reset to ACTIVE.")
        else:
            print(f"[!] Key '{args.key_ref}' not found.", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"[!] Failed to reset key: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_keypool_discover(args):
    pool = KeyPoolManager()
    try:
        discovered = pool.discover_keys()
        if not discovered:
            print("No API keys discovered.")
            return

        print("[+] Discovered API keys:")
        for idx in discovered:
            key_val = get_api_key_by_index(idx)
            if key_val:
                print(f"  Key {idx}: present (length={len(key_val)}, masked)")
            else:
                print(f"  Key {idx}: not found in environment/bashrc")
    except Exception as e:
        print(f"[!] Failed to discover keys: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_config_show(args):
    cli_overrides = {}
    # Add any CLI-specific overrides here
    config = load_config(args.config_path, cli_overrides)
    path = get_config_path(args.config_path)
    print(f"=== Configuration ({path}) ===")
    print(f"Default Key Index:       {config.default_key_index}")
    print(f"Cooldown Seconds:        {config.cooldown_seconds}")
    print(f"Max Total Sync Bytes:    {config.max_total_sync_bytes}")
    print(f"Max File Size Bytes:     {config.max_file_size_bytes}")
    print(f"Log Level:               {config.log_level}")
    print(f"Git Auto Checkpoint:     {config.git_auto_checkpoint}")
    print(f"Default Session Timeout: {config.default_session_timeout}")
    print("============================")


def cmd_config_set(args):
    config = load_config(args.config_path)
    try:
        # Update the specified field
        if hasattr(config, args.key):
            # Type conversion based on field type
            field_type = type(getattr(config, args.key))
            if field_type == bool:
                value = args.value.lower() in ('true', '1', 'yes')
            elif field_type == int:
                value = int(args.value)
            else:
                value = args.value
            setattr(config, args.key, value)
            
            if save_config(config, args.config_path):
                print(f"[+] Config '{args.key}' set to '{value}'.")
            else:
                print(f"[!] Failed to save config.", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"[!] Unknown config key: {args.key}", file=sys.stderr)
            print("Available keys: default_key_index, cooldown_seconds, max_total_sync_bytes, max_file_size_bytes, log_level, git_auto_checkpoint, default_session_timeout")
            sys.exit(1)
    except ValueError as e:
        print(f"[!] Invalid value: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_config_init(args):
    config = RouterConfig()
    if save_config(config, args.config_path):
        path = get_config_path(args.config_path)
        print(f"[+] Default configuration created at {path}")
    else:
        print(f"[!] Failed to create config.", file=sys.stderr)
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
    parser.add_argument("--config", dest="config_path", type=str, default=None, help="Path to config file (default: ~/.agy-router/config.json)")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # test-agent
    test_parser = subparsers.add_parser("test-agent", help="Test Antigravity Agent connection")
    test_parser.add_argument("--key-index", type=int, default=1, help="1-based API key index (default: 1)")
    test_parser.add_argument("--prompt", type=str, default=None, help="Custom test prompt")
    test_parser.set_defaults(func=cmd_test_agent)

    # project
    project_parser = subparsers.add_parser("project", help="Manage projects (static metadata only)")
    project_sub = project_parser.add_subparsers(dest="project_cmd", help="Project commands")
    reg_parser = project_sub.add_parser("register", help="Register a directory as a project")
    reg_parser.add_argument("path", type=str, help="Path to project directory")
    reg_parser.add_argument("--project-id", type=str, default=None, help="Custom project ID")
    reg_parser.set_defaults(func=cmd_project_register)
    status_parser = project_sub.add_parser("status", help="Show project static metadata")
    status_parser.add_argument("--project-id", type=str, default=None, help="Project ID")
    status_parser.set_defaults(func=cmd_project_status)
    list_parser = project_sub.add_parser("list", help="List all registered projects")
    list_parser.set_defaults(func=cmd_project_list)

    # session
    session_parser = subparsers.add_parser("session", help="Manage persistent sessions (runtime state)")
    session_sub = session_parser.add_subparsers(dest="session_cmd", help="Session commands")
    
    s_create = session_sub.add_parser("create", help="Create a new session")
    s_create.add_argument("--project-id", type=str, required=True, help="Project ID")
    s_create.add_argument("--session-id", type=str, default=None, help="Custom session ID")
    s_create.add_argument("--bound-key", type=str, default="key1", help="Bound key reference (default: key1)")
    s_create.add_argument("--environment-id", type=str, default="", help="Environment ID")
    s_create.add_argument("--tenant-id", type=str, default="", help="Tenant ID")
    s_create.set_defaults(func=cmd_session_create)
    
    s_list = session_sub.add_parser("list", help="List sessions")
    s_list.add_argument("--project-id", type=str, default=None, help="Filter by project ID")
    s_list.set_defaults(func=cmd_session_list)
    
    s_status = session_sub.add_parser("status", help="Show session status")
    s_status.add_argument("session_id", type=str, help="Session ID")
    s_status.set_defaults(func=cmd_session_status)
    
    s_update = session_sub.add_parser("update", help="Update session runtime state")
    s_update.add_argument("session_id", type=str, help="Session ID")
    s_update.add_argument("--bound-key", type=str, default=None, help="Bound key reference")
    s_update.add_argument("--environment-id", type=str, default=None, help="Environment ID")
    s_update.add_argument("--last-interaction-id", type=str, default=None, help="Last interaction ID")
    s_update.add_argument("--tenant-id", type=str, default=None, help="Tenant ID")
    s_update.add_argument("--state", type=str, choices=["IDLE", "ACTIVE", "INVALIDATED"], default=None, help="Session state")
    s_update.set_defaults(func=cmd_session_update)
    
    s_invalidate = session_sub.add_parser("invalidate", help="Invalidate a session")
    s_invalidate.add_argument("session_id", type=str, help="Session ID")
    s_invalidate.set_defaults(func=cmd_session_invalidate)

    # key-pool
    keypool_parser = subparsers.add_parser("key-pool", help="Manage key pool (discovery, status, rotation)")
    keypool_sub = keypool_parser.add_subparsers(dest="keypool_cmd", help="Key pool commands")
    
    kp_list = keypool_sub.add_parser("list", help="List all keys with status")
    kp_list.set_defaults(func=cmd_keypool_list)
    
    kp_status = keypool_sub.add_parser("status", help="Show detailed key pool status")
    kp_status.set_defaults(func=cmd_keypool_status)
    
    kp_reset = keypool_sub.add_parser("reset", help="Reset a key to ACTIVE")
    kp_reset.add_argument("key_ref", type=str, help="Key reference (e.g., key1)")
    kp_reset.set_defaults(func=cmd_keypool_reset)
    
    kp_discover = keypool_sub.add_parser("discover", help="Discover available API keys from environment/bashrc")
    kp_discover.set_defaults(func=cmd_keypool_discover)

    # config
    config_parser = subparsers.add_parser("config", help="Manage configuration")
    config_sub = config_parser.add_subparsers(dest="config_cmd", help="Config commands")
    
    c_show = config_sub.add_parser("show", help="Show current configuration")
    c_show.add_argument("--config-path", type=str, default=None, help="Config file path")
    c_show.set_defaults(func=cmd_config_show)
    
    c_set = config_sub.add_parser("set", help="Set a configuration value")
    c_set.add_argument("key", type=str, help="Config key")
    c_set.add_argument("value", type=str, help="Config value")
    c_set.add_argument("--config-path", type=str, default=None, help="Config file path")
    c_set.set_defaults(func=cmd_config_set)
    
    c_init = config_sub.add_parser("init", help="Create default config file")
    c_init.add_argument("--config-path", type=str, default=None, help="Config file path")
    c_init.set_defaults(func=cmd_config_init)

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