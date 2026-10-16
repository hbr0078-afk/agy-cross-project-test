# Phase 8-2.1 — Web UI Exposure Review (documentation only)

Status: **findings only**. No UI refactor is included in the runtime-wiring fix.

## Scope of the files

`web_ui.py` and `web_ui_agent_result.json` were introduced by commit
`5d00619` ("chore(poc): preserve Phase 8-1 global key-pool web dashboard
artifact"), which sits **below** the Phase 8-2 feature commit. They are a
preserved Phase 8-1 POC artifact, not part of the request-level round-robin
feature. They are intentionally **not removed or rewritten** here.

## Findings

1. **Binds to all interfaces.** `main()` starts `ThreadedHTTPServer(("0.0.0.0", PORT))`,
   so the dashboard listens on every network interface, not just loopback.
2. **Wildcard CORS.** Every response (and the `OPTIONS` preflight) sends
   `Access-Control-Allow-Origin: *`, so any origin can read the API.
3. **Unauthenticated management endpoints.** `POST /api/discover` and
   `POST /api/reset` invoke CLI operations (`key-pool discover`, `key-pool reset`)
   with no authentication or authorization check.
4. **Secret-adjacent output.** `/api/keys` and `/api/discover` surface key pool
   state and discovery output. Values are masked today, but the surface is
   unauthenticated.
5. **`web_ui_agent_result.json`** is a saved agent response containing a real
   `environment_id` and `interaction_id` plus token usage. These are not API
   secrets, but they are operational identifiers that should not be published.

## Required hardening (before any public exposure)

- Bind to `127.0.0.1` by default; make the host configurable and require an
  explicit flag for non-loopback binding.
- Replace wildcard CORS with an allowlist, or drop CORS entirely for a
  same-origin dashboard.
- Add authentication (token/session) to **all** `/api/*` routes, and gate
  mutating endpoints (`discover`, `reset`) behind it.
- Never return raw key material; keep the existing masking and add output
  redaction on the CLI passthrough.
- Treat `web_ui_agent_result.json` as a POC artifact: keep it out of public
  releases or strip operational identifiers.

## Recommendation

Keep `web_ui.py` / `web_ui_agent_result.json` out of any production/release
branch until the items above are addressed. Doing so is **out of scope** for
the Phase 8-2.1 runtime-wiring change and is tracked here rather than mixed into
the round-robin integration commit.
