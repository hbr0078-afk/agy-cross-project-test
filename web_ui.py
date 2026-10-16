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

        normalized.append({
            "key_ref": str(ref),
            "index": parsed_index,
            "state": state,
            "fail_count": fail_count,
            "last_used": str(last_used),
        })

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
            <th style="width: 140px;">State</th>
            <th style="width: 120px;">Fail Count</th>
            <th>Last Used</th>
          </tr>
        </thead>
        <tbody id="keysTableBody">
          <tr>
            <td colspan="5" class="empty-state">Loading key pool...</td>
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
        tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No matching keys found in pool.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(k => {
        const stateLower = String(k.state || '').toLowerCase();
        let badgeClass = 'badge-neutral';
        if (stateLower === 'active') badgeClass = 'badge-active';
        else if (stateLower.includes('cool')) badgeClass = 'badge-cooldown';
        else if (stateLower.includes('fail') || stateLower.includes('exhaust')) badgeClass = 'badge-error';

        const failClass = Number(k.fail_count) > 0 ? 'fail-positive' : 'fail-zero';

        return `
          <tr>
            <td class="mono" style="color: var(--slate);">${k.index ?? '-'}</td>
            <td class="mono" style="font-weight: 600; color: #f1f5f9;">${escapeHtml(k.key_ref || '-')}</td>
            <td><span class="badge ${badgeClass}">${escapeHtml(k.state || 'UNKNOWN')}</span></td>
            <td class="mono ${failClass}">${k.fail_count ?? 0}</td>
            <td class="mono" style="color: var(--slate);">${escapeHtml(k.last_used || 'never')}</td>
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

        if path == "/api/discover":
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