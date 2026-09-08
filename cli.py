#!/usr/bin/env python3
import sys
import argparse
from keys import get_api_key_by_index
from client import AntigravityClient

def cmd_test_agent(args):
    # 1. Retrieve active key (Index 1 default)
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

def main():
    parser = argparse.ArgumentParser(prog="agy-router", description="Antigravity Multi-Key Router")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # test-agent subcommand
    test_parser = subparsers.add_parser("test-agent", help="Test Antigravity Agent connection and environment creation")
    test_parser.add_argument("--key-index", type=int, default=1, help="1-based API key index to test (default: 1)")
    test_parser.add_argument("--prompt", type=str, default=None, help="Custom test prompt")
    test_parser.set_defaults(func=cmd_test_agent)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)

if __name__ == "__main__":
    main()
