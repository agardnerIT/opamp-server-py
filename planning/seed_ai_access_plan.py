#!/usr/bin/env python3
"""
Seed the "AI agent accessibility" work plan into a local SQLite database.

DB: planning/ai-access-plan.db  (gitignored via *.db)
Reproducible: rerun to rebuild from scratch (drops and recreates).

Schema:
  approaches  - the four candidate strategies + why each is useful
  gaps        - AI-accessibility gaps found in the codebase
  work_items  - planned tasks, phased, each addressing gaps
  decisions   - recorded trade-off decisions with reasoning
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "ai-access-plan.db"


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE approaches (
            id              TEXT PRIMARY KEY,          -- 'api' | 'cli' | 'mcp' | 'skills'
            name            TEXT NOT NULL,
            what            TEXT NOT NULL,
            why_useful      TEXT NOT NULL,             -- why it helps AI LLM agents
            audience        TEXT NOT NULL,             -- which agents/clients benefit
            effort          TEXT NOT NULL,             -- S / M / L
            value           TEXT NOT NULL,             -- Low / Medium / High
            dependencies    TEXT NOT NULL,
            role_in_plan    TEXT NOT NULL              -- recommendation + build order
        );

        CREATE TABLE gaps (
            id              TEXT PRIMARY KEY,          -- G1..Gn
            title           TEXT NOT NULL,
            detail          TEXT NOT NULL,
            file_ref        TEXT NOT NULL,             -- where in the codebase
            blocks          TEXT NOT NULL              -- which approaches it blocks
        );

        CREATE TABLE work_items (
            id              INTEGER PRIMARY KEY,
            phase           TEXT NOT NULL,             -- '0-api', '1-client', '2-cli', '3-mcp', '4-skills', 'x-cross'
            title           TEXT NOT NULL,
            approach        TEXT NOT NULL REFERENCES approaches(id),
            effort          TEXT NOT NULL,             -- S / M / L
            priority        INTEGER NOT NULL,          -- 1 = highest
            status          TEXT NOT NULL DEFAULT 'planned',
            gaps_addressed  TEXT NOT NULL,             -- comma-separated gap ids
            description     TEXT NOT NULL,
            acceptance      TEXT NOT NULL              -- how we know it's done
        );

        CREATE TABLE decisions (
            id              INTEGER PRIMARY KEY,
            topic           TEXT NOT NULL,
            decision        TEXT NOT NULL,
            alternatives    TEXT NOT NULL,
            reasoning       TEXT NOT NULL,
            timestamp       TEXT DEFAULT (datetime('now'))
        );
        """
    )

    conn.executemany(
        "INSERT INTO approaches (id, name, what, why_useful, audience, effort, value, dependencies, role_in_plan) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "api",
                "Raw REST API + documentation",
                "Complete/verify the FastAPI surface (manifest generation, metadata filtering, reports), keep OpenAPI (/openapi.json, /docs) accurate, and document it in the README.",
                "The lingua franca: every agent framework can speak plain HTTP with zero client support. FastAPI's auto-generated OpenAPI lets agents self-discover endpoints/shapes, eliminating hallucinations. It is also the substrate every other layer wraps.",
                "Every agent (Claude, ChatGPT, Cursor, Copilot, pi, LangChain, custom), plus humans and curl/scripts; enables remote/networked operations.",
                "M",
                "High (foundational)",
                "FastAPI (already present), no new deps",
                "PHASE 0 - foundation. Nothing else is trustworthy until the API is complete and documented.",
            ),
            (
                "cli",
                "Command-line interface (opampctl)",
                "opampctl agents list | get <id> | compliance | policies | alerts | manifest | health  -- emitting JSON.",
                "Shell access is universal in coding agents: zero client configuration beyond PATH, lowest-friction on-ramp. Deterministic JSON output is what agents parse most reliably. Composable with jq/etc. Offline DB mode for incident forensics. Doubles as a test harness for the shared client.",
                "Every coding agent with a shell tool, plus humans and shell scripts; local and remote (point at a running server URL).",
                "M",
                "High",
                "opamp_client (Phase 1); the REST API",
                "PHASE 2 - fast, universal, scriptable layer. Same code backs the MCP server.",
            ),
            (
                "mcp",
                "MCP server (Model Context Protocol)",
                "FastMCP server exposing typed tools: list_agents, get_agent, get_agent_metrics, check_compliance, list_policies, generate_manifest, get_alert_config, send_test_alert...",
                "Native function-calling ergonomics: MCP-capable clients introspect tool schemas in-context, call with structured args, and get structured errors. More reliable than CLI parsing for multi-step workflows. One implementation reaches every MCP client; mcp 1.27.2 already installed.",
                "MCP-capable clients: Claude Desktop/Code, Cursor, VS Code, Copilot, OpenCode, pi and other agent frameworks.",
                "M",
                "High (for MCP-native agents)",
                "opamp_client (Phase 1); the REST API",
                "PHASE 3 - best agent ergonomics; the ecosystem's consolidating standard.",
            ),
            (
                "skills",
                "Agent skills / knowledge files",
                "A SKILL.md-style file plus updates to AGENTS.md/CLAUDE.md teaching agents HOW and WHEN to use the project: run server, connect collector, use CLI vs MCP vs API, and the known gotchas.",
                "Schemas say what exists; skills say how/when to use it. Encodes procedural knowledge (e.g. collector connect sequence) and hard-won quirks from memory.md (st.page_link KeyError, str.format vs JSON braces, st.rerun clearing errors). Works in non-MCP frameworks via plain markdown.",
                "Frameworks with skills/instructions support: Claude Code, pi, Copilot, ChatGPT projects, generic system prompts; also human onboarding.",
                "S-M",
                "Medium-High (multiplier on everything else)",
                "CLI + MCP exist so the skill can point at real tooling",
                "PHASE 4 - wraps the stack together; cheapest layer, biggest 'how to' payoff.",
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO gaps (id, title, detail, file_ref, blocks) VALUES (?,?,?,?,?)",
        [
            (
                "G1",
                "Manifest generation is UI-only",
                "OCB manifest.yaml generation (the flagship 'build minimal collectors' feature) exists only as ui/manifest.py functions called from Streamlit. No REST endpoint, so agents cannot build collector manifests programmatically.",
                "ui/manifest.py (generate_manifest, generate_ocb_command)",
                "api, cli, mcp",
            ),
            (
                "G2",
                "No metadata filtering on GET /agents",
                "UI filters agents by metadata (e.g. environment: production) but GET /agents returns a flat list with no query params (environment, healthy, remote_config_status). Agents must post-filter client-side.",
                "server/main.py:list_agents; ui/pages/1_Agents.py",
                "api, cli, mcp",
            ),
            (
                "G3",
                "Alert config is in-memory only",
                "ALERT_CONFIG dict in alerts.py is never persisted; PUT /alerts config is lost on restart. Also DISPATCHERS only registers webhook, leaving _send_slack/_send_discord/_send_telegram as dead code.",
                "server/alerts.py (ALERT_CONFIG, DISPATCHERS)",
                "api (persist + docs), cli, mcp",
            ),
            (
                "G4",
                "Admin auth is bespoke and undocumented",
                "require_admin() does ad-hoc Basic auth against ADMIN_PASSWORD env var; OpenAPI/README don't describe which endpoints need it or how agents should supply it.",
                "server/main.py:require_admin; README.md endpoints table",
                "api (docs), cli, mcp",
            ),
            (
                "G5",
                "Reports are UI-only",
                "generate_agent_report / generate_heavy_collectors_report / generate_outdated_collectors_report live in ui/shared.py with no API equivalent; a useful agent capability (fleet analysis) is trapped in Streamlit.",
                "ui/shared.py (report generators)",
                "api, cli, mcp",
            ),
            (
                "G6",
                "Sparse endpoint documentation",
                "README has a 6-row endpoint table but no API reference, no link to FastAPI /docs, and many endpoints lack docstrings. Agents have little to anchor on besides code reading.",
                "README.md; server/main.py (sparse docstrings)",
                "api (docs), skills",
            ),
            (
                "G7",
                "No CORS configuration",
                "No CORSMiddleware configured. Browser-based agent harnesses calling the API cross-origin may be blocked (server-side agents are unaffected, but it blocks UI-adjacent automation).",
                "server/main.py (no CORSMiddleware)",
                "api (minor)",
            ),
            (
                "G8",
                "Agent metrics are volatile",
                "AGENT_METRICS is an in-memory dict; restart loses all ingested OTLP metrics even though agent state is persisted in SQLite. Agents doing trend analysis will see empty data after restarts.",
                "server/main.py (AGENT_METRICS)",
                "api, cli, mcp",
            ),
            (
                "G9",
                "No shared client package",
                "ui/shared.py hand-rolls requests calls duplicated across the UI; there is no installable/versioned client both CLI and MCP can share, and no release packaging (pyproject.toml).",
                "ui/shared.py; requirements.txt",
                "cli, mcp (both need a client)",
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO work_items (phase, title, approach, effort, priority, status, gaps_addressed, description, acceptance) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "0-api",
                "Add manifest generation endpoint",
                "api",
                "M",
                1,
                "planned",
                "G1",
                "POST /agent/{agent_id}/manifest -> {manifest_yaml, ocb_command, collector_version}. Reuse ui/manifest.py logic by moving it into server/ (or a shared module) so UI and API call the same code.",
                "curl-able endpoint returns valid manifest.yaml + ocb command; UI still works; unit tests cover manifest content.",
            ),
            (
                "0-api",
                "Add metadata filtering to GET /agents",
                "api",
                "S",
                1,
                "planned",
                "G2",
                "Query params: ?environment=prod&healthy=true&status=UNSET. Filter on description metadata attributes.",
                "Filtered results match UI Agents page behavior; documented in OpenAPI.",
            ),
            (
                "0-api",
                "Persist alert config to SQLite",
                "api",
                "S",
                2,
                "planned",
                "G3",
                "Extend SQLiteAgentStore (or a new AlertsStore) to persist ALERT_CONFIG; load at startup; resolve/dispatch dead dispatcher code (webhook only for now).",
                "Restart server, alert config survives; GET/PUT /alerts work against persisted state.",
            ),
            (
                "0-api",
                "Add report endpoints",
                "api",
                "M",
                2,
                "planned",
                "G5",
                "POST /reports/heavy-collectors and /reports/outdated-collectors (plus per-agent markdown reports) reusing ui/shared.py report generators moved to server/.",
                "Reports return the same data/shape the UI shows; tests cover payloads.",
            ),
            (
                "0-api",
                "Document the API (docs pass)",
                "api",
                "M",
                1,
                "planned",
                "G4,G6,G7",
                "Docstrings on every endpoint; README API reference section + link to /docs; document Basic auth (ADMIN_PASSWORD) and which endpoints require it; add CORSMiddleware (configurable via env).",
                "An agent can read README or fetch /openapi.json and correctly call every endpoint incl. auth.",
            ),
            (
                "0-api",
                "Persist agent metrics to SQLite",
                "api",
                "S",
                3,
                "planned",
                "G8",
                "Store AGENT_METRICS in SQLite (latest snapshot per agent; optionally a recent-samples table). Fall back to in-memory if store unavailable.",
                "Metrics survive restart and are reachable via existing /agent/{id}/metrics.",
            ),
            (
                "1-client",
                "Move shared logic into server-agnostic modules",
                "api",
                "M",
                1,
                "planned",
                "G1,G5",
                "Lift generate_manifest + report generators out of ui/ into server/ (or lib/), have UI import from there (keeps UI behavior identical).",
                "No UI regressions; server imports share the same functions.",
            ),
            (
                "1-client",
                "Build opamp_client library",
                "api",
                "M",
                1,
                "planned",
                "G9",
                "client/opamp_client.py: httpx-based client, Basic auth, JSON everywhere, typed methods mirroring the API, structured errors, --server URL from env (OPAMP_SERVER_URL).",
                "CLI and MCP server both use it; no double-implemented HTTP logic.",
            ),
            (
                "1-client",
                "Package the project for installation",
                "api",
                "M",
                3,
                "planned",
                "G9",
                "pyproject.toml with entry point (opampctl) + optional mcp extra; pip install -e . works; update requirements files.",
                "pip install -e . gives a working opampctl and importable opamp_server package.",
            ),
            (
                "2-cli",
                "Build opampctl CLI",
                "cli",
                "M",
                2,
                "planned",
                "G1,G2,G4,G5,G8",
                "Subcommands: agents (list/filter), agent (get/metrics/compliance/manifest), policies, alerts (get/update/test), health, reports. JSON output default; --db flag for offline SQLite read mode.",
                "Every documented API call has a CLI equivalent emitting valid JSON; README examples work.",
            ),
            (
                "2-cli",
                "CLI docs + completion",
                "cli",
                "S",
        4,
                "planned",
                "G6",
                "README quickstart (install, first commands), agent-facing examples, tab completion notes.",
                "A fresh agent can run three commands from the README without errors.",
            ),
            (
                "3-mcp",
                "Build MCP server (FastMCP)",
                "mcp",
                "M",
        2,
                "planned",
                "G1,G2,G4,G5,G8",
                "mcp_server/ exposing tools mirroring opamp_client methods; stdio transport default, optional SSE; env config OPAMP_SERVER_URL / ADMIN_PASSWORD.",
                "Claude/Cursor connect via mcpServers.json; each tool call round-trips correctly; tools listed with descriptions.",
            ),
            (
                "3-mcp",
                "MCP client config snippets in README",
                "mcp",
                "S",
        3,
                "planned",
                "G6",
                "mcpServers.json examples for Claude Desktop, Claude Code, Cursor; note on remote/SSE usage.",
                "Copy-paste config works on a fresh machine.",
            ),
            (
                "4-skills",
                "Write opamp-server agent skill",
                "skills",
                "M",
        4,
                "planned",
                "G4,G6",
                "skills/opamp-server/SKILL.md: when to use, quickstart (server + collector bring-up, incl. 'ignore initial collector errors' nuance), CLI vs MCP vs API guidance, gotchas from memory.md.",
                "An agent loading the skill can bring up server+collector and run end-to-end checks.",
            ),
            (
                "4-skills",
                "Update AGENTS.md + CLAUDE.md",
                "skills",
                "S",
        4,
                "planned",
                "G6",
                "Add 'AI accessibility' section pointing at the CLI/MCP/skill; keep build/test commands accurate.",
                "Project agent docs reference the new tooling; no stale command lists.",
            ),
            (
                "x-cross",
                "Tests for new surface",
                "api",
                "M",
        2,
                "planned",
                "-",
                "Extend tests/ (existing test_server.py / test_state.py patterns): endpoint tests for manifest/filter/reports/alerts-persistence; client tests; MCP tool tests against a live test instance.",
                "pytest tests/ -v green; new endpoints covered.",
            ),
            (
                "x-cross",
                "Final QA + README refresh",
                "api",
                "S",
        5,
                "planned",
                "-",
                "End-to-end walkthrough as an agent would experience it; README screenshots of new UI if touched; confirm Docker images still build.",
                "A fresh agent follows README and reaches a working state without human help.",
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO decisions (topic, decision, alternatives, reasoning) VALUES (?,?,?,?)",
        [
            (
                "Strategy",
                "Build all four, layered: API+docs (phase 0) -> shared client (1) -> CLI (2) -> MCP server (3) -> skills (4).",
                "Pick only one (CLI, MCP, API-only, or skills-only).",
                "They are not competitors: the API is the trusted substrate, the client prevents duplicated logic, CLI gives universal cheap access, MCP gives the best agent ergonomics where supported, skills encode how/when knowledge. Building in this order means each layer costs less and reuses the previous one. If budget is constrained: API alone; API+MCP if two.",
            ),
            (
                "Shared client",
                "One opamp_client library used by both the CLI and the MCP server; UI can adopt it later.",
                "Duplicate HTTP/auth logic per tool; or have the MCP server shell out to the CLI.",
                "Single source of truth for auth, URL handling, error types and JSON parsing keeps behavior consistent and testable. MCP-shelling-to-CLI adds process overhead and loses structured errors.",
            ),
            (
                "CLI output",
                "JSON by default (agents parse JSON best); human-friendly flag optional.",
                "Pretty human tables by default.",
                "Primary consumers are LLM agents; deterministic JSON is the most reliably parseable format. Keep a --pretty option for humans.",
            ),
            (
                "MCP transport",
                "stdio by default (works with every local MCP client); optional SSE for remote.",
                "SSE-only; or HTTP streams.",
                "stdio has zero networking config and is the default for Claude/Cursor/etc. SSE is additive for remote deployments later, matching how the FastAPI server is already network-exposed.",
            ),
            (
                "Auth",
                "Keep existing Basic auth (ADMIN_PASSWORD) and document it for agents via env var guidance; do not introduce a new auth system.",
                "API keys, OAuth, no auth.",
                "Consistency with the running server and zero migration cost. Agents pass OPAMP_ADMIN_PASSWORD / use a --password flag. No auth on read-only endpoints (already the case).",
            ),
            (
                "CLI framework",
                "typer/click for opampctl (rich help is better for agents) — light dependency.",
                "argparse (zero deps); raw sys.argv parsing.",
                "Rich, greppable --help output materially improves agent usability, and typer is a small dependency consistent with the existing Python stack.",
            ),
        ],
    )

    conn.commit()
    conn.close()
    print(f"Seeded plan DB at {DB_PATH}")


if __name__ == "__main__":
    main()