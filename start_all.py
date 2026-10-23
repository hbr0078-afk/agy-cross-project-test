#!/usr/bin/env python3
"""agy-router 크로스 플랫폼 런처 (Windows / Linux).

- ~/.bashrc 에서 export AGY_KEY_... / GEMINI_API_KEY... 를 os.environ 으로 로드
- 어댑터(8765) + Web UI(8080) 를 백그라운드 서브프로세스로 기동
- 1.5초 후 http://localhost:8080 브라우저 오픈
- Ctrl+C 로 두 프로세스 모두 종료
"""
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(ROOT, "openclaw-test-provider", "adapter", "adapter.py")
WEB_UI = os.path.join(ROOT, "web_ui.py")


def load_bashrc_env():
    path = os.path.expanduser("~/.bashrc")
    if not os.path.exists(path):
        return
    pat = re.compile(r"^export\s+(AGY_KEY_\d+|GEMINI_API_KEY(?:_\d+)?)=(.+)$")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = pat.match(line.strip())
            if m:
                val = m.group(2).strip().strip("\"'").strip()
                if val and not os.environ.get(m.group(1)):
                    os.environ[m.group(1)] = val


def main():
    load_bashrc_env()

    print("[agy-router] Starting adapter (port 8765) / Web UI (port 8080) ...")
    print("[agy-router] 어댑터(8765) 및 Web UI(8080) 시작 중 ...")
    procs = [
        subprocess.Popen([sys.executable, ADAPTER], cwd=os.path.dirname(ADAPTER)),
        subprocess.Popen([sys.executable, WEB_UI], cwd=ROOT),
    ]

    def open_browser():
        time.sleep(1.5)
        webbrowser.open("http://localhost:8080")

    threading.Thread(target=open_browser, daemon=True).start()

    print("[agy-router] Web UI: http://localhost:8080  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
            if all(p.poll() is not None for p in procs):
                print("[agy-router] Both processes exited.")
                break
    except KeyboardInterrupt:
        print("\n[agy-router] Stopping...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
