# Decisions

## Design Principles

- As simple as possible. As complex as necessary.

## Commit/Issue Protocol

- "Use 'bd' for task tracking" and open an issue on GitHub
- When a task is done, close it using 'bd' and write a summary to the Git issue then close it out.
- Never write sensitive information to Github issues
- Link issues to commits (use "Closes #XX" or "Refs #XX" in commit message)
- NEVER close issues until the user confirms they're resolved
- Always add a comment to issues and comments that this work was assisted by AI
- Compact your memory after we close each issue

## Issue Handling

- When asked to do an issue, FIRST look it up and understand it
- Create as mant subtasks as you need to simplify work
- Fix the issue and add/update comments on the issue
- Link commits to the issue but DO NOT close until explicitly told
- This applies to all future issue work

## Scope Changes

- If scope changes (because user wasn't clear enough in issue), update the original issue/comment to explain the new scope and why you're doing more work

## Environment Variables

- All new configurable settings should be added to `.env.sample`

## Planning Process

- When planning an issue, post the plan to the GitHub issue BEFORE modifying any code
- Wait for human review/approval before actioning
- You can always create subtasks if a task is too big / if it's easier

## Issue Closing

- ALWAYS push commits after closing so the fix shows in the issue
- User must be able to see which commit/PR closed the issue

## Additional Ideas

- ALWAYS ask for clarification if needed
- ALWAYS prompt user with ideas they may not have thought of

## Config Push (epic #93)

- Work starts in new sessions. Read epic #93 first: threat model, design decisions D1-D9, and the dependency graph are all there. Do not relitigate a decision without saying why in the issue.
- **Nothing ships before the security prerequisites** (#94 agent auth, #95 admin auth, #96 fail-closed posture). Config push on today's server = fleet takeover.
- Two-phase flow is mandatory: **propose → approve → apply**, with `approver` as a separate role from `operator` (that is what makes approval meaningful).
- **MCP may only propose/validate.** Apply/approve/rollback are never exposed to MCP, and write tools are absent (not failing) unless `OPAMP_MCP_ALLOW_WRITES=true`.
- Never auto-retry a `FAILED` or auto-reverted config: poison the revision, require re-approval.
- A **redacted** config body must be un-pushable. Pushing one silently wipes credentials fleet-wide.
- Agent-side reality (verified against upstream): remote config is applied by the **OpAMP Supervisor**, not by `extension/opampextension` (it never advertises `AcceptsRemoteConfig`). The supervisor persists `last_recv_remote_config.dat` + `last_working_remote_config.dat` and auto-reverts on failure, which is why naive hash reconciliation loops.

## Commits

- NEVER commit to git until user confirms issue is resolved
- NEVER push without explicit user approval

## UI Changes

- Whenever there are UI changes, take screenshots and add them to the README so users always see the latest and greatest UI
- Before taking screenshots: start a collector (`~/tools/otelcol-contrib --config=collector/config.yaml`). Wait 5 seconds then start the UI. Disregard the initial collector error logs. That's expected as the collector cannot connect.

## Dependencies

- Always keep dependencies up to date with latest versions
- Check for updates regularly and update requirements files accordingly

# Technical Discoveries

- Config push posture lives in `server/posture.py` (#96): every flag read at call time; `push_allowed()` = flag on AND posture met (or `OPAMP_ALLOW_INSECURE_CONFIG_PUSH=true`). `assert_startup_posture()` is called from `lifespan` and from `__main__`; uvicorn exits **3** on lifespan startup failure. Gate for push routes = `Depends(posture.config_push_gate)` → **404 "config push is disabled"** (never 403). `posture.scrub_remote_config(response)` is the last-line guard for #81/#102 code paths.
- Server-side remote-config capability is `ServerCapabilities_OffersRemoteConfig` (the agent side is `AgentCapabilities_AcceptsRemoteConfig`) — the server must NOT advertise it while push is disallowed.
- Admin credential resolution moved to `server/admin_auth.py` (#96): `ADMIN_PASSWORD` → `ADMIN_PASSWORD_FILE` → generated (`OPAMP_ADMIN_AUTOGENERATE_PASSWORD=true`, persisted `$DATA_DIR/admin_password` 0600, printed once). `ADMIN_PASSWORD` is module state now — tests patch `server.admin_auth.ADMIN_PASSWORD`, **not** `server.main.ADMIN_PASSWORD`. Empty credential = read-only mode (privileged endpoints still open; #95 closes that).
- loguru colour markup (`<red>`) is **not** stripped for file/uvicorn sinks here — startup banners must be plain text or the tags appear literally.
- Default ports in `docker-compose.yml` bind to `127.0.0.1` on purpose; TLS examples in `docker/nginx.conf` + `docker/Caddyfile` (Upgrade/Connection, long read timeout, Origin passthrough). `data/admin_password` is git- and docker-ignored.

- `st.page_link()` throws `KeyError: 'url_pathname'` with string paths or improperly resolved paths in this project structure
- `str.format()` conflicts with JSON braces in alert body templates; use `str.replace()` instead
- Streamlit multi-page apps require `st.set_page_config()` in each page file
- Duplicate `st.form()` keys cause `StreamlitAPIException`; use unique keys per page/context
- `st.rerun()` immediately after `st.error()` clears the error; use session state flags or `st.stop()` to persist errors
- Test alert button should autosave config before sending, otherwise unsaved templates are ignored
- Generated protobuf code (`proto/opamp_pb2.py`) requires runtime >= the protoc gencode version; if tests die at collection with `google.protobuf.runtime_version.VersionError`, upgrade: `pip install -U 'protobuf>=7.34.1'` (7.36.1 on this machine)
- Manifest generation lives in `server/manifest.py` (moved from `ui/manifest.py`) — shared by `POST /agent/{id}/manifest` and the UI; user-supplied versions must go through `validate_manifest_version()`
- `opentelemetry-collector-contrib` facts that shape config push: (a) `extension/opampextension` never sets `AcceptsRemoteConfig` — a bare collector cannot apply pushed config, only the **OpAMP Supervisor** can (`cmd/opampsupervisor`: composes config files, restarts collector via SIGHUP, persists `last_recv_remote_config.dat`/`last_working_remote_config.dat`, auto-reverts with `restoreLastWorkingRemoteConfig`); (b) the extension's documented transport is now **WebSocket** (`server.ws.endpoint`), with `headers`, an `auth` extension and `tls` available on both transports — so per-agent token auth is feasible without inventing wire extensions; (c) upstream warns an invalid config leaves the collector "down until manually restarted" (opamp extension no longer running), so rollout staging is a safety requirement, not polish; (d) `reports_raw_config` can expose secrets in the reported effective config
- `GET /agents` supports query filtering (matching logic in `AgentState.matches_filters()`/`get_attr()` in `server/state.py`): `healthy=true|false|unknown` (Literal → 422 on bad values), `status=`/`remote_config_status=` (remote config, case-insensitive, reserved names win over metadata), and any other param filters description attributes (identifying + non-identifying, stringified AnyValue, case-insensitive, repeated params = OR); response includes a `filters` echo
- Phase 0 of the AI-access epic (#47–#52) is COMPLETE. Remaining epic: #53 server-agnostic modules → #54 opamp_client → #55 packaging → #56/#57 CLI → #58/#59 MCP → #60/#61 skills → #62/#63 QA
- CORS via `CORS_ORIGINS` env (comma-separated, default `*`); `allow_credentials` must be False when wildcard (browsers reject `*` + credentials); parsing helper `_parse_cors_origins()` in `server/main.py`
- Alert config + agent metrics persist in `data/opamp.db`: `alert_config` table (single-row KV, id=1) loaded/saved by `server/alerts.py`; `agent_metrics` table (latest snapshot per agent) via `SQLiteMetricsStore` in `server/state.py`; all store ops best-effort (warn, never raise) so in-memory fallback works
- Report generators live in `server/reports.py` (moved from `ui/shared.py`, which re-imports for the UI pages); endpoints: `GET /reports/fleet`, `/reports/heavy-collectors?threshold=`, `/reports/outdated-collectors?version=`, `/agent/{id}/report`; `_is_heavy` uses strict `>` (exactly 0.5 is not heavy); `parse_version("garbage")` returns `()` not `(0,)`
- Every route in `server/main.py` has a docstring + `tags` (auth/opamp/telemetry/agents/compliance/alerts/reports/ops) + `summary` — keep this convention for new endpoints; auth-gating is invisible to OpenAPI so it must be stated in the docstring
- Dead slack/discord/telegram/cloudevents alert dispatchers were removed (#50) — webhook is the only type; re-add as real `ALERT_TYPES` entries if needed later
- Test conventions: store tests use `tmp_path` (never the repo's `data/opamp.db`); live smoke tests via `uvicorn` on ports 4395-4399, then **clean up seeded rows** from the local DB afterwards- AI-access epic (#64) is COMPLETE — all 17 sub-issues (#47-#63) closed. Layered stack: REST API → `client/opamp_client.py` (httpx, `OPAMP_SERVER_URL`/`ADMIN_PASSWORD`, `OpampApiError(.status_code,.detail)`) → `cli/main.py` (`opampctl` typer, JSON default, `--raw`, `--db` offline SQLite mode) → `mcp_server/` (FastMCP, stdio default, 19 tools) → `skills/opamp-server/SKILL.md`
- Keep all four agent-facing layers in sync when adding endpoints: endpoint + client method + opampctl command + MCP tool, each with tests (AGENTS.md/CLAUDE.md now document this rule)
- `mcp` extra must be pinned `<2`: mcp 2.x renamed FastMCP→MCPServer (breaking)
- starlette 1.6's TestClient transport targets httpx 2.x and breaks with httpx 0.28 — tests use `tests/asgi_transport.py` `InProcessASGITransport` (sync anyio-based ASGI dispatch) instead of TestClient internals; to test CLI/MCP/client layers, monkeypatch the factory (`cli.main._client` / `mcp_server.server._client`) with an `OpampClient(transport=InProcessASGITransport(server_app))`
- Packaging: `pyproject.toml` packages `client`, `cli`, `server`, `proto`, `mcp_server`; entry points `opampctl = cli.main:app` (Typer object is callable), `opamp-mcp = mcp_server.server:main`
- CLI `--db` offline mode reuses `server.state` stores (SQLiteAgentStore.load_all, SQLiteMetricsStore) + server.manifest/server.reports pure functions; metrics entries are shaped `{"metrics": {...}, "updated_at": ...}` (API-shaped, not flat)
