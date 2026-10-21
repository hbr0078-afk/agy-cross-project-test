#!/usr/bin/env bash
# Launch the Antigravity adapter with the FULL key pool loaded from ~/.bashrc.
# Keys are read from ~/.bashrc (never echoed). Multi-key rollover requires >=2.
set -u
cd /home/ubuntu/agy-router/openclaw-test-provider

# Export every AGY_KEY_N / GEMINI_API_KEY(_N) found in ~/.bashrc
while IFS= read -r line; do
  case "$line" in
    export\ AGY_KEY_*=*|export\ GEMINI_API_KEY=*|export\ GEMINI_API_KEY_*=*)
      # shellcheck disable=SC2163
      eval "export ${line#export }"
      ;;
  esac
done < <(grep -E '^export (AGY_KEY_[0-9]+|GEMINI_API_KEY(_[0-9]+)?)=' "$HOME/.bashrc")

n=$(env | grep -cE '^(AGY_KEY_[0-9]+|GEMINI_API_KEY(_[0-9]+)?)=')
echo "[launcher] exported $n key(s) from ~/.bashrc"

export AGY_ADAPTER_DEBUG=1
exec python3 adapter/adapter.py
