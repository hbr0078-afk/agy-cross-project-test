#!/usr/bin/env python3
import sys
import os
import argparse
from keys import get_api_key_by_index
from client import AntigravityClient
from registry import ProjectRegistry, RegistryCorruptedError

def cmd_test_agent(args):
    api_key = get_api_key_by_index(args.key_index)
    if not api_key:
        print(f"Error: No API key found for index {args.key_index} (Checked AGY_KEY_{args.key_index}, GEMINI_API_KEY_{args.key_index}, GEMINI_API_KEY).", file=sys.stderr)
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
            # Detect by current working directory
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

def main():
    parser = argparse.ArgumentParser(prog="agy-router", description="Antigravity Multi-Key Router")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # test-agent subcommand
    test_parser = subparsers.add_parser("test-agent", help="Test Antigravity Agent connection and environment creation")
    test_parser.add_argument("--key-index", type=int, default=1, help="1-based API key index to test (default: 1)")
    test_parser.add_argument("--prompt", type=str, default=None, help="Custom test prompt")
    test_parser.set_defaults(func=cmd_test_agent)

    # project subcommands
    project_parser = subparsers.add_parser("project", help="Manage projects and persistent sessions")
    project_sub = project_parser.add_subparsers(dest="project_cmd", help="Project commands")

    # project register
    reg_parser = project_sub.add_parser("register", help="Register a directory as a project")
    reg_parser.add_argument("path", type=str, help="Path to project directory")
    reg_parser.add_argument("--project-id", type=str, default=None, help="Custom project ID (defaults to folder name)")
    reg_parser.set_defaults(func=cmd_project_register)

    # project status
    status_parser = project_sub.add_parser("status", help="Show project status and environment details")
    status_parser.add_argument("--project-id", type=str, default=None, help="Project ID (defaults to current directory lookup)")
    status_parser.set_defaults(func=cmd_project_status)

    # project list
    list_parser = project_sub.add_parser("list", help="List all registered projects")
    list_parser.set_defaults(func=cmd_project_list)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)

if __name__ == "__main__":
    main()
