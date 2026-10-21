#!/usr/bin/env python3
"""
agy-router - Web UI Dashboard
Lightweight, single-file management dashboard and REST API for Antigravity API key router.
Standard library ONLY: http.server, urllib, json, subprocess, os, sys.
"""

import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import time
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8080))
DEFAULT_PROJECT_DIR = "/home/ubuntu/agy-router"


def find_cli_path():
    """Locate the cli.py file in common project locations."""
    candidates = [
        os.path.join(os.getcwd(), "cli.py"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py"),
        os.path.join(DEFAULT_PROJECT_DIR, "cli.py"),
        os.path.expanduser("~/agy-router/cli.py"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return "cli.py"


def run_cli(*args):
    """Execute a CLI command within the project context."""
    cli_path = find_cli_path()
    work_dir = (
        os.path.dirname(cli_path)
        if os.path.isabs(cli_path) and os.path.exists(cli_path)
        else (DEFAULT_PROJECT_DIR if os.path.isdir(DEFAULT_PROJECT_DIR) else os.getcwd())
    )
    cmd = [sys.executable, cli_path, *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "command": " ".join(cmd),
        }
    except Exception as exc:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "command": " ".join(cmd),
        }


def normalize_keys(data):
    """Normalize arbitrary JSON or dictionary formats into a standard key array."""
    raw_list = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        if "keys" in data and isinstance(data["keys"], list):
            raw_list = data["keys"]
        elif "keys" in data and isinstance(data["keys"], dict):
            for k, v in data["keys"].items():
                if isinstance(v, dict):
                    item = dict(v)
                    item.setdefault("key_ref", k)
                    raw_list.append(item)
                else:
                    raw_list.append({"key_ref": k, "state": str(v)})
        elif "key_pool" in data and isinstance(data["key_pool"], list):
            raw_list = data["key_pool"]
        else:
            for k, v in data.items():
                if isinstance(v, dict):
                    item = dict(v)
                    if "key_ref" not in item and "ref" not in item and "key" not in item:
                        item["key_ref"] = k
                    raw_list.append(item)
                else:
                    raw_list.append({"key_ref": k, "state": str(v)})
    else:
        return []

    normalized = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            normalized.append({
                "key_ref": str(item),
                "index": idx,
                "state": "active",
                "fail_count": 0,
                "last_used": "never",
            })
            continue

        ref = (
            item.get("key_ref")
            or item.get("keyRef")
            or item.get("ref")
            or item.get("name")
            or item.get("id")
            or item.get("key")
            or f"key-{idx}"
        )
        raw_index = item.get("index") if item.get("index") is not None else item.get("idx", idx)
        try:
            parsed_index = int(raw_index)
        except (ValueError, TypeError):
            parsed_index = raw_index

        state = str(item.get("state") or item.get("status") or item.get("health") or "active").lower()

        raw_fails = (
            item.get("fail_count")
            if item.get("fail_count") is not None
            else item.get("failCount", item.get("fails", item.get("failures", 0)))
        )
        try:
            fail_count = int(raw_fails)
        except (ValueError, TypeError):
            fail_count = 0

        last_used = (
            item.get("last_used")
            or item.get("lastUsed")
            or item.get("last_used_at")
            or item.get("lastUsedAt")
            or item.get("updated_at")
            or "never"
        )
        if isinstance(last_used, (int, float)) and last_used > 0:
            last_used = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_used))

        tenant_id = item.get("tenant_id") or item.get("tenantId") or ""

        normalized.append({
            "key_ref": str(ref),
            "index": parsed_index,
            "state": state,
            "fail_count": fail_count,
            "last_used": str(last_used),
            "tenant_id": str(tenant_id),
        })

    normalized.sort(key=lambda x: x["index"] if isinstance(x["index"], int) else 9999)
    return normalized


def parse_cli_key_list(text):
    """Parse text/table output of `python3 cli.py key-pool list`."""
    if not text:
        return []

    try:
        parsed = json.loads(text)
        return normalize_keys(parsed)
    except Exception:
        pass

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    clean_lines = [
        line for line in lines
        if not re.match(r"^[\+\-\=\| ]+$", line) and not line.startswith("+---")
    ]
    if not clean_lines:
        return []

    headers = []
    header_idx = -1
    for i, line in enumerate(clean_lines):
        lower = line.lower()
        if "ref" in lower or "index" in lower or "state" in lower or "key" in lower:
            header_idx = i
            if "|" in line:
                headers = [c.strip().lower() for c in line.split("|") if c.strip()]
            else:
                headers = [c.strip().lower() for c in re.split(r"\s{2,}|\t", line) if c.strip()]
            break

    data_lines = clean_lines[header_idx + 1:] if header_idx != -1 else clean_lines
    results = []

    for idx, line in enumerate(data_lines):
        if "|" in line:
            cols = [c.strip() for c in line.split("|") if c.strip()]
        else:
            cols = [c.strip() for c in re.split(r"\s{2,}|\t", line) if c.strip()]
            if len(cols) == 1:
                cols = line.split()

        if not cols:
            continue

        item = {
            "key_ref": f"key-{idx}",
            "index": idx,
            "state": "active",
            "fail_count": 0,
            "last_used": "never",
        }

        if headers and len(headers) == len(cols):
            for h, col in zip(headers, cols):
                if any(x in h for x in ["ref", "key", "name", "id"]):
                    item["key_ref"] = col
                elif any(x in h for x in ["index", "idx"]):
                    item["index"] = int(col) if col.isdigit() else col
                elif any(x in h for x in ["state", "status"]):
                    item["state"] = col.lower()
                elif any(x in h for x in ["fail", "error"]):
                    item["fail_count"] = int(col) if col.isdigit() else 0
                elif any(x in h for x in ["used", "time", "last"]):
                    item["last_used"] = col
        else:
            if cols[0].isdigit() and len(cols) > 1:
                item["index"] = int(cols[0])
                item["key_ref"] = cols[1]
                if len(cols) > 2:
                    item["state"] = cols[2].lower()
                if len(cols) > 3:
                    item["fail_count"] = int(cols[3]) if cols[3].isdigit() else 0
                if len(cols) > 4:
                    item["last_used"] = " ".join(cols[4:])
            else:
                item["key_ref"] = cols[0]
                if len(cols) > 1:
                    item["index"] = int(cols[1]) if cols[1].isdigit() else cols[1]
                if len(cols) > 2:
                    item["state"] = cols[2].lower()
                if len(cols) > 3:
                    item["fail_count"] = int(cols[3]) if cols[3].isdigit() else 0
                if len(cols) > 4:
                    item["last_used"] = " ".join(cols[4:])

        results.append(item)

    return results


def get_keys_data():
    """Retrieve keys from key_states.json or fallback to CLI."""
    file_candidates = [
        os.path.expanduser("~/.agy-router/key_states.json"),
        "/home/ubuntu/.agy-router/key_states.json",
        os.path.join(DEFAULT_PROJECT_DIR, "key_states.json"),
    ]
    for path in file_candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return normalize_keys(data)
            except Exception:
                pass

    res = run_cli("key-pool", "list")
    return parse_cli_key_list(res["stdout"])


def get_health_data():
    """Execute health check command and format as JSON."""
    res = run_cli("health")
    raw_out = res["stdout"]
    try:
        data = json.loads(raw_out)
        if isinstance(data, dict):
            if "success" not in data:
                data["success"] = res["success"]
            if "output" not in data:
                data["output"] = raw_out
            return data
        return {"success": res["success"], "data": data, "output": raw_out}
    except Exception:
        return {
            "success": res["success"],
            "status": "healthy" if res["success"] else "unhealthy",
            "output": raw_out or (res["stderr"] if not res["success"] else "OK"),
            "error": res["stderr"],
            "returncode": res["returncode"],
        }


def _discover_bashrc_indices(bashrc_path):
    indices = set()
    if not os.path.exists(bashrc_path):
        return indices
    pat = re.compile(r"^export\s+(?:AGY_KEY_|GEMINI_API_KEY_)(\d+)=")
    with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if s.startswith("export GEMINI_API_KEY="):
                indices.add(1)
            m = pat.match(s)
            if m:
                indices.add(int(m.group(1)))
    return indices


def add_key_to_pool(api_key, index=None, tenant_id=""):
    """Add or update an AGY_KEY_<index> in ~/.bashrc, key_states.json, and restart adapter."""
    api_key = (api_key or "").strip()
    tenant_id = (tenant_id or "").strip()
    if not api_key or not re.match(r"^[A-Za-z0-9_\-]+$", api_key):
        return {"success": False, "error": "유효한 API Key 형식이 아닙니다."}

    bashrc_path = os.path.expanduser("~/.bashrc")
    existing_indices = _discover_bashrc_indices(bashrc_path)

    if index is not None and str(index).strip() != "":
        try:
            idx = int(index)
            if idx < 1 or idx > 999:
                return {"success": False, "error": "Index는 1~999 사이여야 합니다."}
        except ValueError:
            return {"success": False, "error": "Index는 숫자여야 합니다."}
    else:
        idx = 1
        while idx in existing_indices:
            idx += 1

    lines = []
    if os.path.exists(bashrc_path):
        with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

    target_prefix = f"export AGY_KEY_{idx}="
    new_line = f'export AGY_KEY_{idx}="{api_key}"\n'
    replaced = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(target_prefix):
            if not replaced:
                new_lines.append(new_line)
                replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(new_line)

    tmp_bashrc = bashrc_path + ".tmp-webui"
    with open(tmp_bashrc, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.chmod(tmp_bashrc, 0o600)

    chk = subprocess.run(["bash", "-n", tmp_bashrc], capture_output=True, text=True)
    if chk.returncode != 0:
        os.remove(tmp_bashrc)
        return {"success": False, "error": f"bashrc 문법 검증 실패: {chk.stderr}"}

    os.replace(tmp_bashrc, bashrc_path)
    os.chmod(bashrc_path, 0o600)

    # Update tenants.env if tenant_id provided
    if tenant_id:
        tenants_path = os.path.expanduser("~/.agy-router/tenants.env")
        t_lines = []
        if os.path.exists(tenants_path):
            with open(tenants_path, "r", encoding="utf-8", errors="ignore") as f:
                t_lines = f.readlines()
        t_prefix = f"AGY_TENANT_{idx}="
        t_replaced = False
        new_t_lines = []
        for tl in t_lines:
            if tl.strip().startswith(t_prefix) or tl.strip().startswith(f"export {t_prefix}"):
                if not t_replaced:
                    new_t_lines.append(f"AGY_TENANT_{idx}={tenant_id}\n")
                    t_replaced = True
            else:
                new_t_lines.append(tl)
        if not t_replaced:
            if new_t_lines and not new_t_lines[-1].endswith("\n"):
                new_t_lines[-1] += "\n"
            new_t_lines.append(f"AGY_TENANT_{idx}={tenant_id}\n")
        with open(tenants_path, "w", encoding="utf-8") as f:
            f.writelines(new_t_lines)
        os.chmod(tenants_path, 0o600)

    # Update key_states.json immediately
    state_path = os.path.expanduser("~/.agy-router/key_states.json")
    os.makedirs(os.path.dirname(state_path), mode=0o700, exist_ok=True)
    state_data = {"keys": {}}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
        except Exception:
            state_data = {"keys": {}}
    if "keys" not in state_data or not isinstance(state_data["keys"], dict):
        state_data["keys"] = {}

    key_ref = f"key{idx}"
    entry = state_data["keys"].get(key_ref, {})
    entry["index"] = idx
    entry["state"] = "ACTIVE"
    entry["fail_count"] = 0
    entry["cooldown_until"] = None
    entry.setdefault("last_used_at", None)
    entry.setdefault("last_success_at", None)
    if tenant_id:
        entry["tenant_id"] = tenant_id
    state_data["keys"][key_ref] = entry

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2, ensure_ascii=False)
    os.chmod(state_path, 0o600)

    disc = run_cli("key-pool", "discover")
    subprocess.run(["systemctl", "--user", "restart", "agy-adapter.service"], capture_output=True)

    return {
        "success": True,
        "key_ref": key_ref,
        "index": idx,
        "output": f"Added {key_ref} (index={idx}) to ~/.bashrc & key_states.json, restarted agy-adapter.service.\n{disc.get('stdout', '')}".strip(),
    }


def delete_key_from_pool(key_ref=None, index=None):
    """Remove a key from ~/.bashrc, key_states.json, tenants.env, and restart adapter."""
    idx = None
    if index is not None and str(index).strip() != "":
        try:
            idx = int(index)
        except ValueError:
            pass
    if idx is None and key_ref:
        m = re.match(r"^key[-_]?(\d+)$", str(key_ref).strip(), re.IGNORECASE)
        if m:
            idx = int(m.group(1))
    if idx is None:
        return {"success": False, "error": "삭제할 키의 유효한 index 또는 key_ref가 필요합니다."}

    key_ref_norm = f"key{idx}"
    bashrc_path = os.path.expanduser("~/.bashrc")
    removed_lines = 0
    if os.path.exists(bashrc_path):
        with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            s = line.strip()
            if s.startswith(f"export AGY_KEY_{idx}=") or s.startswith(f"export GEMINI_API_KEY_{idx}="):
                removed_lines += 1
                continue
            if idx == 1 and s.startswith("export GEMINI_API_KEY="):
                removed_lines += 1
                continue
            new_lines.append(line)
        tmp_bashrc = bashrc_path + ".tmp-webui"
        with open(tmp_bashrc, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.chmod(tmp_bashrc, 0o600)
        chk = subprocess.run(["bash", "-n", tmp_bashrc], capture_output=True, text=True)
        if chk.returncode != 0:
            os.remove(tmp_bashrc)
            return {"success": False, "error": f"bashrc 문법 검증 실패: {chk.stderr}"}
        os.replace(tmp_bashrc, bashrc_path)
        os.chmod(bashrc_path, 0o600)

    # Remove from key_states.json
    state_path = os.path.expanduser("~/.agy-router/key_states.json")
    removed_state = False
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            if isinstance(state_data, dict) and isinstance(state_data.get("keys"), dict):
                if key_ref_norm in state_data["keys"]:
                    del state_data["keys"][key_ref_norm]
                    removed_state = True
                if key_ref and key_ref in state_data["keys"]:
                    del state_data["keys"][key_ref]
                    removed_state = True
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    subprocess.run(["systemctl", "--user", "restart", "agy-adapter.service"], capture_output=True)
    return {
        "success": True,
        "key_ref": key_ref_norm,
        "index": idx,
        "output": f"Deleted {key_ref_norm} (removed {removed_lines} line(s) from ~/.bashrc, state_removed={removed_state}) and restarted agy-adapter.service.",
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>agy-router | Key Pool Dashboard</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: #111827;
      --card-border: #1e293b;
      --primary: #6366f1;
      --emerald: #10b981;
      --emerald-bg: rgba(16, 185, 129, 0.12);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.12);
      --rose: #ef4444;
      --rose-bg: rgba(239, 68, 68, 0.12);
      --slate: #94a3b8;
      --slate-muted: #64748b;
      --text: #f8fafc;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh;
      padding: 1.5rem;
    }
    .container {
      max-width: 1200px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--card-border);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .brand-logo {
      width: 40px;
      height: 40px;
      background: linear-gradient(135deg, #6366f1, #3b82f6);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 1.2rem;
      box-shadow: 0 4px 14px rgba(99, 102, 241, 0.35);
    }
    .brand-text h1 {
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .brand-text p {
      font-size: 0.8rem;
      color: var(--slate);
    }
    .header-status {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      background: rgba(30, 41, 59, 0.6);
      padding: 0.4rem 0.9rem;
      border-radius: 9999px;
      border: 1px solid var(--card-border);
      font-size: 0.82rem;
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--emerald);
      box-shadow: 0 0 8px var(--emerald);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
    }
    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1.1rem;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
    }
    .stat-label {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--slate);
    }
    .stat-value {
      font-size: 1.6rem;
      font-weight: 700;
      color: #fff;
    }
    .controls {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1rem;
    }
    .btn-group {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      flex-wrap: wrap;
    }
    button {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.55rem 1rem;
      font-size: 0.85rem;
      font-weight: 600;
      border-radius: 8px;
      border: 1px solid transparent;
      cursor: pointer;
      transition: all 0.15s ease-in-out;
      background: #1e293b;
      color: var(--text);
    }
    button:hover:not(:disabled) {
      filter: brightness(1.15);
      transform: translateY(-1px);
    }
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none !important;
    }
    .btn-primary {
      background: var(--primary);
      color: #ffffff;
    }
    .btn-warning {
      background: rgba(245, 158, 11, 0.2);
      border-color: rgba(245, 158, 11, 0.4);
      color: #fbbf24;
    }
    .btn-success {
      background: rgba(16, 185, 129, 0.2);
      border-color: rgba(16, 185, 129, 0.4);
      color: #34d399;
    }
    .btn-outline {
      background: transparent;
      border-color: var(--card-border);
      color: var(--slate);
    }
    .btn-outline:hover:not(:disabled) {
      border-color: var(--slate);
      color: #fff;
    }
    .filter-group {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .search-input {
      background: #0f172a;
      border: 1px solid var(--card-border);
      color: #fff;
      padding: 0.55rem 0.85rem;
      border-radius: 8px;
      font-size: 0.85rem;
      outline: none;
      min-width: 220px;
    }
    .search-input:focus {
      border-color: var(--primary);
    }
    .toggle-group {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.82rem;
      color: var(--slate);
    }
    .toggle-group input[type="checkbox"] {
      cursor: pointer;
      accent-color: var(--primary);
    }
    .table-container {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.88rem;
    }
    th {
      background: #0f172a;
      color: var(--slate);
      font-weight: 600;
      padding: 0.85rem 1.1rem;
      border-bottom: 1px solid var(--card-border);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    td {
      padding: 0.85rem 1.1rem;
      border-bottom: 1px solid var(--card-border);
      color: #e2e8f0;
    }
    tr:last-child td {
      border-bottom: none;
    }
    tr:hover td {
      background-color: rgba(255, 255, 255, 0.02);
    }
    .mono {
      font-family: var(--font-mono);
      font-size: 0.84rem;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 0.25rem 0.65rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .badge-active {
      background: var(--emerald-bg);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .badge-cooldown {
      background: var(--amber-bg);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }
    .badge-error, .badge-exhausted {
      background: var(--rose-bg);
      color: #f87171;
      border: 1px solid rgba(239, 68, 68, 0.3);
    }
    .badge-neutral {
      background: rgba(148, 163, 184, 0.1);
      color: var(--slate);
      border: 1px solid rgba(148, 163, 184, 0.2);
    }
    .fail-zero {
      color: var(--slate-muted);
    }
    .fail-positive {
      color: #f87171;
      font-weight: 700;
    }
    .empty-state {
      padding: 3rem;
      text-align: center;
      color: var(--slate);
    }
    .terminal-card {
      background: #060911;
      border: 1px solid var(--card-border);
      border-radius: 12px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .terminal-header {
      background: #0c121e;
      padding: 0.6rem 1rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--card-border);
      font-size: 0.78rem;
      color: var(--slate);
    }
    .terminal-body {
      padding: 0.9rem 1.1rem;
      font-family: var(--font-mono);
      font-size: 0.8rem;
      color: #cbd5e1;
      max-height: 180px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .toast {
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      padding: 0.85rem 1.25rem;
      border-radius: 10px;
      background: #1e293b;
      color: #fff;
      font-size: 0.85rem;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
      border: 1px solid var(--card-border);
      display: flex;
      align-items: center;
      gap: 0.65rem;
      opacity: 0;
      transform: translateY(15px);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      pointer-events: none;
      z-index: 1000;
    }
    .toast.show {
      opacity: 1;
      transform: translateY(0);
      pointer-events: auto;
    }
    .toast.success { border-color: var(--emerald); }
    .toast.error { border-color: var(--rose); }
    .toast.info { border-color: var(--primary); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="brand-logo">AG</div>
        <div class="brand-text">
          <h1>agy-router</h1>
          <p>Multi-Key Pool Router for Antigravity API</p>
        </div>
      </div>
      <div class="header-status">
        <span class="pulse-dot" id="liveDot"></span>
        <span id="liveStatusText">Router Online</span>
        <span style="color: var(--slate); font-size: 0.75rem; margin-left: 0.5rem;" id="lastRefreshTime"></span>
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Total Keys</div>
        <div class="stat-value" id="statTotalKeys">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Active Keys</div>
        <div class="stat-value" style="color: #34d399;" id="statActiveKeys">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Cooldown / Inactive</div>
        <div class="stat-value" style="color: #fbbf24;" id="statCooldownKeys">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Health Status</div>
        <div class="stat-value" style="font-size: 1.3rem;" id="statHealth">Checking...</div>
      </div>
    </div>

    <div class="controls" style="flex-wrap: wrap;">
      <div class="btn-group" style="align-items: center; gap: 0.75rem;">
        <label style="color: var(--slate); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;">Antigravity Model</label>
        <select id="modelSelect" style="background: #0f172a; color: #f8fafc; border: 1px solid var(--card-border); border-radius: 8px; padding: 0.55rem 0.8rem; font-family: var(--font-mono); font-size: 0.85rem; min-width: 220px;">
          <option value="">Loading...</option>
        </select>
        <button class="btn-primary" id="btnApplyModel" onclick="handleApplyModel()">Apply Model</button>
      </div>
      <div class="btn-group" style="align-items: center; gap: 0.5rem; flex-wrap: wrap;">
        <label style="color: var(--slate); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; margin-right: 0.25rem;">Add Key</label>
        <input type="number" id="newKeyIndex" class="search-input" style="min-width: 90px; width: 90px;" placeholder="Idx (Auto)" min="1" max="999">
        <input type="password" id="newKeyValue" class="search-input" style="min-width: 240px;" placeholder="API Key (AIzaSy...)">
        <input type="text" id="newKeyTenant" class="search-input" style="min-width: 190px;" placeholder="Tenant/Project ID (Optional)">
        <button class="btn-success" id="btnAddKey" onclick="handleAddKey()">
          + Add / Update Key
        </button>
      </div>
    </div>

    <div class="controls">
      <div class="btn-group">
        <button class="btn-primary" id="btnDiscover" onclick="handleDiscover()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
          Discover Keys
        </button>
        <button class="btn-warning" id="btnReset" onclick="handleReset()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
          Reset Cooldowns
        </button>
        <button class="btn-success" id="btnHealth" onclick="handleHealthCheck()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
          Health Check
        </button>
        <button class="btn-outline" id="btnRefresh" onclick="fetchKeys()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
          Refresh
        </button>
      </div>

      <div class="filter-group">
        <input type="text" id="searchInput" class="search-input" placeholder="Filter keys by ref or state..." oninput="renderTable()">
        <label class="toggle-group">
          <input type="checkbox" id="autoRefreshToggle" checked onchange="toggleAutoRefresh(this.checked)">
          Auto-refresh (5s)
        </label>
      </div>
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th style="width: 80px;">Index</th>
            <th>Key Ref</th>
            <th>Tenant / Project</th>
            <th style="width: 140px;">State</th>
            <th style="width: 120px;">Fail Count</th>
            <th>Last Used</th>
            <th style="width: 110px; text-align: right;">Action</th>
          </tr>
        </thead>
        <tbody id="keysTableBody">
          <tr>
            <td colspan="7" class="empty-state">Loading key pool...</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="terminal-card">
      <div class="terminal-header">
        <span>Execution Console</span>
        <span id="terminalStatus">Ready</span>
      </div>
      <div class="terminal-body" id="terminalOutput">Waiting for actions...</div>
    </div>
  </div>

  <div id="toast" class="toast">
    <span id="toastMessage"></span>
  </div>

  <script>
    let keysData = [];
    let autoRefreshTimer = null;

    function showToast(message, type = 'info') {
      const toast = document.getElementById('toast');
      const toastMessage = document.getElementById('toastMessage');
      toastMessage.textContent = message;
      toast.className = `toast show ${type}`;
      clearTimeout(toast._timeout);
      toast._timeout = setTimeout(() => {
        toast.className = 'toast';
      }, 4000);
    }

    function setTerminal(title, content) {
      document.getElementById('terminalStatus').textContent = title;
      document.getElementById('terminalOutput').textContent = content || '(No output returned)';
    }

    async function fetchKeys() {
      try {
        const res = await fetch('/api/keys');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        keysData = Array.isArray(data) ? data : [];
        updateStats();
        renderTable();
        document.getElementById('lastRefreshTime').textContent = new Date().toLocaleTimeString();
      } catch (err) {
        console.error('Failed to fetch keys:', err);
        document.getElementById('keysTableBody').innerHTML = `
          <tr><td colspan="5" class="empty-state" style="color: #f87171;">
            Failed to load key pool: ${err.message}
          </td></tr>`;
      }
    }

    async function fetchHealth() {
      try {
        const res = await fetch('/api/health');
        const data = await res.json();
        const statEl = document.getElementById('statHealth');
        if (data.success || data.status === 'healthy' || data.status === 'ok') {
          statEl.textContent = 'Healthy';
          statEl.style.color = '#34d399';
        } else {
          statEl.textContent = 'Degraded';
          statEl.style.color = '#f87171';
        }
        return data;
      } catch (err) {
        document.getElementById('statHealth').textContent = 'Error';
        document.getElementById('statHealth').style.color = '#f87171';
        throw err;
      }
    }

    function updateStats() {
      document.getElementById('statTotalKeys').textContent = keysData.length;
      const activeCount = keysData.filter(k => (k.state || '').toLowerCase() === 'active').length;
      const cooldownCount = keysData.filter(k => (k.state || '').toLowerCase() !== 'active').length;
      document.getElementById('statActiveKeys').textContent = activeCount;
      document.getElementById('statCooldownKeys').textContent = cooldownCount;
    }

    function renderTable() {
      const query = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      const tbody = document.getElementById('keysTableBody');

      const filtered = keysData.filter(k => {
        if (!query) return true;
        const ref = String(k.key_ref || '').toLowerCase();
        const state = String(k.state || '').toLowerCase();
        const idx = String(k.index || '').toLowerCase();
        return ref.includes(query) || state.includes(query) || idx.includes(query);
      });

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No matching keys found in pool.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(k => {
        const stateLower = String(k.state || '').toLowerCase();
        let badgeClass = 'badge-neutral';
        if (stateLower === 'active') badgeClass = 'badge-active';
        else if (stateLower.includes('cool')) badgeClass = 'badge-cooldown';
        else if (stateLower.includes('fail') || stateLower.includes('exhaust')) badgeClass = 'badge-error';

        const failClass = Number(k.fail_count) > 0 ? 'fail-positive' : 'fail-zero';
        const safeRef = escapeHtml(k.key_ref || '-');
        const safeIdx = k.index ?? '';

        return `
          <tr>
            <td class="mono" style="color: var(--slate);">${k.index ?? '-'}</td>
            <td class="mono" style="font-weight: 600; color: #f1f5f9;">${safeRef}</td>
            <td class="mono" style="color: var(--slate);">${escapeHtml(k.tenant_id || '-')}</td>
            <td><span class="badge ${badgeClass}">${escapeHtml(k.state || 'UNKNOWN')}</span></td>
            <td class="mono ${failClass}">${k.fail_count ?? 0}</td>
            <td class="mono" style="color: var(--slate);">${escapeHtml(k.last_used || 'never')}</td>
            <td style="text-align: right;">
              <button class="btn-outline" style="padding: 0.3rem 0.65rem; font-size: 0.75rem; color: #f87171; border-color: rgba(239,68,68,0.35);" onclick="handleDeleteKey('${safeRef}', '${safeIdx}')">Delete</button>
            </td>
          </tr>
        `;
      }).join('');
    }

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    async function handleDiscover() {
      const btn = document.getElementById('btnDiscover');
      btn.disabled = true;
      showToast('Running key discovery...', 'info');
      setTerminal('Executing', 'python3 cli.py key-pool discover');
      try {
        const res = await fetch('/api/discover', { method: 'POST' });
        const data = await res.json();
        const output = data.output || data.error || (data.success ? 'Success' : 'Failed');
        setTerminal(data.success ? 'Discover Completed' : 'Discover Failed', output);
        showToast(data.success ? 'Key discovery completed' : 'Discovery failed', data.success ? 'success' : 'error');
        await fetchKeys();
      } catch (err) {
        setTerminal('Discover Error', err.message);
        showToast(`Discovery error: ${err.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    async function handleReset() {
      const btn = document.getElementById('btnReset');
      btn.disabled = true;
      showToast('Resetting key cooldowns...', 'info');
      setTerminal('Executing', 'python3 cli.py key-pool reset');
      try {
        const res = await fetch('/api/reset', { method: 'POST' });
        const data = await res.json();
        const output = data.output || data.error || (data.success ? 'Success' : 'Failed');
        setTerminal(data.success ? 'Reset Completed' : 'Reset Failed', output);
        showToast(data.success ? 'Cooldowns reset successfully' : 'Reset failed', data.success ? 'success' : 'error');
        await fetchKeys();
      } catch (err) {
        setTerminal('Reset Error', err.message);
        showToast(`Reset error: ${err.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    async function handleHealthCheck() {
      const btn = document.getElementById('btnHealth');
      btn.disabled = true;
      showToast('Checking router health...', 'info');
      setTerminal('Executing', 'python3 cli.py health');
      try {
        const data = await fetchHealth();
        const output = typeof data === 'object' ? JSON.stringify(data, null, 2) : String(data);
        setTerminal('Health Status', output);
        showToast('Health check complete', data.success !== false ? 'success' : 'error');
      } catch (err) {
        setTerminal('Health Check Error', err.message);
        showToast(`Health check error: ${err.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    async function fetchConfig() {
      try {
        const res = await fetch('/api/config');
        const data = await res.json();
        const sel = document.getElementById('modelSelect');
        const models = data.available_models || [];
        const current = data.model || '';
        sel.innerHTML = models.map(m =>
          `<option value="${escapeHtml(m)}" ${m === current ? 'selected' : ''}>${escapeHtml(m)}</option>`
        ).join('');
        if (current && !models.includes(current)) {
          sel.innerHTML = `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)}</option>` + sel.innerHTML;
        }
      } catch (err) {
        console.error('Failed to fetch config:', err);
        showToast(`Config load error: ${err.message}`, 'error');
      }
    }

    async function handleApplyModel() {
      const btn = document.getElementById('btnApplyModel');
      const model = document.getElementById('modelSelect').value;
      if (!model) {
        showToast('Select a model first', 'error');
        return;
      }
      btn.disabled = true;
      setTerminal('Applying model', model);
      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const data = await res.json();
        if (data.success) {
          setTerminal('Model Applied', `Active Antigravity model is now: ${model}`);
          showToast(`Model switched to ${model}`, 'success');
        } else {
          setTerminal('Model Apply Failed', data.error || 'Unknown error');
          showToast('Failed to apply model', 'error');
        }
      } catch (err) {
        setTerminal('Model Apply Error', err.message);
        showToast(`Apply error: ${err.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    async function handleAddKey() {
      const btn = document.getElementById('btnAddKey');
      const indexVal = document.getElementById('newKeyIndex').value.trim();
      const apiKeyVal = document.getElementById('newKeyValue').value.trim();
      const tenantVal = document.getElementById('newKeyTenant').value.trim();

      if (!apiKeyVal) {
        showToast('API Key를 입력해주세요', 'error');
        return;
      }
      btn.disabled = true;
      setTerminal('Adding Key', `Adding key (index=${indexVal || 'auto'})...`);
      try {
        const res = await fetch('/api/keys/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            index: indexVal ? Number(indexVal) : null,
            api_key: apiKeyVal,
            tenant_id: tenantVal
          }),
        });
        const data = await res.json();
        if (data.success) {
          document.getElementById('newKeyIndex').value = '';
          document.getElementById('newKeyValue').value = '';
          document.getElementById('newKeyTenant').value = '';
          setTerminal('Key Added', data.output || `Added ${data.key_ref}`);
          showToast(`Added ${data.key_ref} successfully`, 'success');
          await fetchKeys();
        } else {
          setTerminal('Add Key Failed', data.error || 'Unknown error');
          showToast(data.error || 'Failed to add key', 'error');
        }
      } catch (err) {
        setTerminal('Add Key Error', err.message);
        showToast(`Add key error: ${err.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    async function handleDeleteKey(keyRef, index) {
      if (!confirm(`정말 ${keyRef} (Index ${index}) 키를 풀에서 삭제할까요?`)) return;
      setTerminal('Deleting Key', `Deleting ${keyRef} (index=${index})...`);
      try {
        const res = await fetch('/api/keys/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key_ref: keyRef, index: index }),
        });
        const data = await res.json();
        if (data.success) {
          setTerminal('Key Deleted', data.output || `Deleted ${keyRef}`);
          showToast(`Deleted ${keyRef} successfully`, 'success');
          await fetchKeys();
        } else {
          setTerminal('Delete Key Failed', data.error || 'Unknown error');
          showToast(data.error || 'Failed to delete key', 'error');
        }
      } catch (err) {
        setTerminal('Delete Key Error', err.message);
        showToast(`Delete key error: ${err.message}`, 'error');
      }
    }

    function toggleAutoRefresh(enabled) {
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      if (enabled) {
        autoRefreshTimer = setInterval(() => {
          fetchKeys();
        }, 5000);
      }
    }

    window.addEventListener('DOMContentLoaded', () => {
      fetchKeys();
      fetchHealth();
      fetchConfig();
      toggleAutoRefresh(true);
    });
  </script>
</body>
</html>
"""


class RouterHandler(http.server.BaseHTTPRequestHandler):
    """HTTP Request Handler for Router Dashboard and API endpoints."""

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code, html_str):
        body = html_str.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_html(200, DASHBOARD_HTML)
        elif path == "/api/config":
            try:
                cfg_path = os.path.expanduser("~/.agy-router/config.json")
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f)
                else:
                    cfg_data = {"model": "gemini-4-argon"}
                cfg_data["available_models"] = [
                    "gemini-4-argon",
                    "gemini-3.8-flash",
                    "gemini-3.7-flash",
                    "gemini-3.5-flash",
                    "gemini-2.5-flash"
                ]
                self._send_json(200, cfg_data)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif path == "/api/keys":
            keys = get_keys_data()
            self._send_json(200, keys)
        elif path == "/api/health":
            health = get_health_data()
            self._send_json(200, health)
        else:
            self._send_json(404, {"error": "Not Found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(length) if length else b"{}"
                data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                
                cfg_path = os.path.expanduser("~/.agy-router/config.json")
                cfg_dict = {}
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_dict = json.load(f)
                
                if "model" in data:
                    cfg_dict["model"] = data["model"]
                
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_dict, f, indent=2)
                
                self._send_json(200, {"success": True, "config": cfg_dict})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif path == "/api/discover":
            res = run_cli("key-pool", "discover")
            self._send_json(200, {
                "success": res["success"],
                "returncode": res["returncode"],
                "output": res["stdout"],
                "error": res["stderr"],
                "command": res["command"],
            })
        elif path == "/api/reset":
            res = run_cli("key-pool", "reset")
            self._send_json(200, {
                "success": res["success"],
                "returncode": res["returncode"],
                "output": res["stdout"],
                "error": res["stderr"],
                "command": res["command"],
            })
        elif path == "/api/keys/add":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(length) if length else b"{}"
                data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                res = add_key_to_pool(
                    api_key=data.get("api_key", ""),
                    index=data.get("index"),
                    tenant_id=data.get("tenant_id", ""),
                )
                self._send_json(200 if res.get("success") else 400, res)
            except Exception as e:
                self._send_json(500, {"success": False, "error": str(e)})
        elif path == "/api/keys/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(length) if length else b"{}"
                data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                res = delete_key_from_pool(
                    key_ref=data.get("key_ref"),
                    index=data.get("index"),
                )
                self._send_json(200 if res.get("success") else 400, res)
            except Exception as e:
                self._send_json(500, {"success": False, "error": str(e)})
        else:
            self._send_json(404, {"error": "Not Found", "path": path})

    def log_message(self, format, *args):
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args[0]} {args[1]} {args[2]}\n")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    server = ThreadedHTTPServer(("0.0.0.0", PORT), RouterHandler)
    print(f"agy-router Web UI listening on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


if __name__ == "__main__":
    main()