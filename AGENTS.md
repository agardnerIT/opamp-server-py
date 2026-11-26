# Agent Instructions

## AI Accessibility (driving this project programmatically)

This project is designed to be operated by AI agents. Layers (each wraps the previous):

1. **REST API** — FastAPI at `:4320`, self-documenting: `GET /openapi.json` (machine schema), `/docs` (Swagger). Endpoint table in `docs/api-reference.md`, auth model in `docs/security.md`.
2. **`opamp_client`** (Python) — `from client import OpampClient`; reads `OPAMP_SERVER_URL` / `ADMIN_PASSWORD`. Structured errors: `OpampApiError(.status_code, .detail)`, `OpampConnectionError`.
3. **`opampctl` CLI** — JSON output by default, `--raw` for markdown/YAML, `--db data/opamp.db` for offline SQLite reads. Config: `--server`/`--password` or env.
4. **MCP server** — `python -m mcp_server` (stdio; `--transport sse` for remote). 39 typed tools by default (47 with `OPAMP_MCP_ALLOW_WRITES=true`); needs `pip install -e ".[mcp]"`. Client config snippets in `docs/mcp.md`.
5. **Agent skill** — `skills/opamp-server/SKILL.md`: when/how guidance, quickstart, gotchas.
6. **Agent selection** — SQL over the live fleet (#132): `GET /agents?q=...`, `GET /agents/query/schema` (columns + values live agents report), `opampctl agents list --query`, MCP `query_agents`. Groups are saved queries. Language guide: `docs/sql-agent-queries.md`.

When adding API endpoints: add the endpoint (docstring included), extend `client/opamp_client.py`, add an `opampctl` command, add an MCP tool, then add tests for each. All four layers must stay in sync.

Config bodies are secrets: never render one without `server/redaction.py` (one
filter, everywhere), never store one without `server/crypto.py` (AES-GCM, bound to
the row), and never accept a body containing `<redacted>` (a redacted push wipes
fleet credentials). `AgentState.to_dict()` is the API choke point — redaction
happens there, not per route.

## Nothing Proprietary (standing decision, 2026-11-21, #150)

**This project does not — and will never — implement, speak, or support a
proprietary interface.** If a tool or agent wants support, it MUST implement
OpAMP according to the OpenTelemetry spec standards. Nothing justifies building
for proprietary surfaces.

What this means in practice:

- Agent support is a **profile** (`server/profiles/data/*.yaml`) plus, at most,
  standard-OpAMP client configuration templates — never a new transport or
  protocol code path.
- Vendor SDKs/agents that only speak a vendor's own API are **out of scope
  permanently** — do not scope, spike, or capture for them. Example: Grafana
  Alloy's River configuration is served over Grafana's proprietary `remotecfg`
  ConnectRPC API (grafana/alloy#1236 open); it stays unsupported. What IS
  supported is the same product's standard-OpAMP path (`alloy otel-supervisor`
  manual mode, stock upstream supervisor).
- A vendor's "simple mode"/cloud-only configuration that bypasses standard
  OpAMP is also out of scope (e.g. Alloy's GCLOUD_* simple mode).
- If a product has no standard-OpAMP path, the answer is "open an issue on
  their repo", not "add a shim here".

## LOCAL-ONLY planning: `planning/supervisor-driver-model/` (standing note, 2026-11-24)

The supervisor driver-model plan in `planning/supervisor-driver-model/` is
**LOCAL TEST ONLY — do NOT commit or push it.** An earlier session pushed it
to `origin` by mistake; it has been recalled from remote history. It is
gitignored so `git add -A` / `git add .` will not pick it up — keep it that
way. If / when the approach is proven, it moves to **its own repo**
(working name `opamp-agent`); until then no session should commit it here,
reference it from committed docs, or open the upstream PR draft it contains.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **Run quality gates** (if code changed) - Tests, linters, builds
2. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
3. **Clean up** - Clear stashes, prune remote branches
4. **Verify** - All changes committed AND pushed
5. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
