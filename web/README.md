# OpAMP Console — frontend

React 19 + TypeScript (strict) + Vite + Tailwind v4 + shadcn/ui (Radix) +
TanStack Query/Router. See `../new-ui-plans.md` for the staged plan; this UI
replaces the Streamlit UI (`ui/`) page by page until full parity.

**All stages through 9 are done** (auth, agents, remote config, read pages,
admin pages, test + screenshot parity) — this UI is at feature parity with
Streamlit and ready for the stage-10 cutover sign-off. Screenshots below are
the current console, regenerated per the [Screenshots](#screenshots) section.

## Development

```bash
npm install
npm run dev            # http://localhost:8501 (product port), proxies /api -> 127.0.0.1:4320
```

Port 8501 is Streamlit's old port — the owner wants the console on it
(2027-ui Stage B). `strictPort` is false, so if the compose stack's Streamlit
UI currently owns 8501 the dev server starts on the next port up instead of
refusing; at cut-over the port is fully handed over.

The FastAPI server must be running for live data (see `../startDevStack.sh`).
Without it, run against the built-in fixtures:

```bash
VITE_USE_MOCK_API=1 npm run dev
```

## Quality gates

```bash
npm run build   # tsc -b (strict) + vite build
npm run test    # vitest (always mock transport — no live server needed)
npm run lint    # oxlint
npm run e2e     # Playwright (mock-mode dev server: auth, agents, stepper, admin, roles)
npm run e2e:live # Playwright live smoke (boots a throwaway read-only server on :4330)
```

`e2e:live` is the mock suite's complement: it starts a real FastAPI server
(temp `DATA_DIR`, throwaway `ADMIN_PASSWORD`, config push off) on port 4330 —
never the dev stack's server on 4320 — and walks the real cookie round-trip
(login → shell → privileged write → logout → revoked cookie). Fleet-dependent
views stay in the mock suite, where fixtures make them deterministic.

## Screenshots

Every user-visible screen has a capture under `../docs/screenshots/`, linked
from the section below. Regenerate all of them with the mock dev server up:

```bash
VITE_USE_MOCK_API=1 npm run dev -- --port 5199 --strictPort &
node tools/screenshot-shell.mjs          # nav-admin, agents-session, login-error
node tools/screenshot-agents.mjs         # agents list, collector detail
node tools/screenshot-read-pages.mjs     # groups, reports
node tools/screenshot-remote-config.mjs  # the config stepper
node tools/screenshot-admin.mjs          # alerts, tokens, sessions, compliance, updates
node tools/screenshot-quickstart.mjs     # zero-agent quickstart + connected Overview
```

## Auth (cookie sessions, stage 3)

The browser authenticates with the httpOnly `opamp_admin_session` cookie the
server sets on `POST /auth/session` (bearer tokens stay for CLI/API clients).
Consequences baked into this codebase:

- The session token is **never** in JS state: `login()` in `endpoints.ts`
  discards it, nothing is persisted to `localStorage`, and identity/role/expiry
  come from `GET /auth/me` via the `useSession` hook (`src/api/session.ts`).
- A relogin-classified error (401) anywhere drops session state and routes to
  `/login` via the global handler registered in `src/main.tsx` — a failed login
  form is also a 401, but it is already on `/login` and surfaces inline.
- "Extend" in the session slot is the audited re-auth (`extendSession`): fresh
  credentials, DELETE old session, then POST the new one. The order is inverted
  from the Streamlit flow because the cookie must still point at the old session
  when the DELETE lands.

Latest screens (mock-mode fleet fixtures, refreshed through 2027-ui Stage B —
Stage A design system; Stage B moved `/` to an Overview dashboard and the
agents table to `/agents`. The zero-agent screens are the quickstart: the
`opamp-mock-empty-fleet` switch in `src/api/mock.ts` renders them without a
second dev server, and `tools/screenshot-quickstart.mjs` captures all three):

| | |
|---|---|
| ![Overview](../docs/screenshots/console-overview.png) | ![Agents list](../docs/screenshots/console-agents-list.png) |
| Overview (`/`): the fleet graphic — a measured, drawing line per agent, each chip carrying its heartbeat age — then health counts, type breakdown, config activity, entry cards | Agents list (`/agents`): server-side SQL selection, health chips, schema columns, type + provenance badges, last-seen ages |
| ![Zero-agent quickstart](../docs/screenshots/console-overview-empty.png) | ![Token minted inline](../docs/screenshots/console-overview-token.png) |
| Zero agents (`/`): the server/fleet graphic with dashed sockets, and the mint form inline — no trip through Agent tokens | Same page after minting: the once-only token, paste-ready snippet, endpoint, and a live wait that flips the page when the first agent checks in |
| ![Agent detail](../docs/screenshots/console-agent-detail.png) | |
| Agent detail: profile-declared telemetry panels, component inventory, masked config flagged | |
| ![Agents with session slot](../docs/screenshots/console-agents-session.png) | ![Login error](../docs/screenshots/console-login-error.png) |
| Agents list + session slot (role, expiry, Extend, Sign out) | Login with the 401 notice |
| ![Nav parity with Admin section](../docs/screenshots/console-nav-admin.png) | |
| Sidebar at nav parity: main pages + role-gated Admin section, active-nav marker | |
| ![Remote Config stepper](../docs/screenshots/console-remote-config.png) | |
| Remote Config: target → edit → review → propose → approve, with the rollout monitor below after creation | |
| ![Groups](../docs/screenshots/console-groups.png) | ![Reports](../docs/screenshots/console-reports.png) |
| Groups: search → save → inspect (kind, pins, drift) → preview/edit/delete, all server-resolved | Reports: the fleet's profiles decide which reports are offered; the server's markdown, rendered and downloadable |
| ![Alerts](../docs/screenshots/console-alerts.png) | ![Agent tokens](../docs/screenshots/console-agent-tokens.png) |
| Alerts: per-event webhooks, Switch-enabled, masked secrets kept on save | Agent tokens: mint (shown once) and revoke, state pills |
| ![Sessions](../docs/screenshots/console-sessions.png) | ![Updates](../docs/screenshots/console-updates.png) |
| Sessions: every honoured admin session, per-row revoke | Updates: release checks, automatic-check switch, GitHub releases link |

## Overview and the zero-agent quickstart

`/` is the only page with two shapes, chosen by `GET /agents`:

- **Fleet empty** → the server/fleet graphic beside `ConnectFirstAgent`: label
  (optional), agent **type**, and an **expiry** picker whose default option is
  literally "Never (default)" — the server's own default, stated rather than
  implied (with a hint line spelling out what no expiry means). Then one primary
  **Generate API token**. The mint is the server's own `POST /agent-tokens`, so
  the response carries the once-only token, its expiry, the `endpoint` and the
  paste-ready `collector_snippet` for the chosen profile — the page holds no
  snippet copy of its own. After minting, the panel polls `GET /agents` every 5s
  (and offers **Check again**) and announces the first agent by name; when one
  connects, the panel goes away and the page becomes the connected Overview.
- **Fleet connected** → the same graphic as a compact hero above the counts.

`components/FleetTopology.tsx` draws one line per agent into a **measured**
layout: it reads its own DOM boxes (ResizeObserver) and asks
`lib/topology.ts` for each path, so the line meets the node edge at any width,
with no magic numbers. A new agent's line draws itself in (dash offset on
`pathLength=1`, staggered by `--i`) and then carries a travelling spark; the
zero-agent state shows the same sockets with dashed stubs instead. All of that
is CSS in `index.css` and is disabled by `prefers-reduced-motion`.

The zero-agent screens are reachable in mock mode without a second server:
`localStorage.setItem("opamp-mock-empty-fleet", "1")` makes the mock's
`GET /agents` answer an empty fleet (`src/api/mock.ts`); `e2e/quickstart.spec.ts`
uses it for the whole journey (empty → mint → agent appears → connected hero).

## Read pages: Reports, Groups, Help (stage 7)

- **Reports** (`src/routes/Reports.tsx`): the server generates the markdown
  (`GET /reports/fleet`, `/reports/heavy-collectors`, `/reports/outdated-collectors`);
  the page renders it and offers the download. Which reports are offered is the
  union of what the fleet's profiles declare (`lib/reportTypes.ts`) — a fleet
  with no component concept is never offered "Heavy agents".
- **Groups** (`src/routes/Groups.tsx`): a group is a **saved SQL selection** —
  search first (the same `SqlSearch` box as Agents), then name it. Running and
  previewing are viewer actions; save/edit/delete need `operator` (hinted, the
  server re-authorises). The detail panel shows selector vs pins (`sources`),
  unknown pins, health-defined warnings and the drift count; delete confirms and
  surfaces a live-rollout 409. Full CRUD lives in `src/api/groups.ts`.
- **Help** (`src/routes/Help.tsx`): the universal connect facts (endpoint +
  agent token), the profile dictionary from `GET /agent/types`, token model, the
  SQL selection language (`docs/sql-agent-queries.md` is the full reference) and
  the session/role security model.
- The report markdown subset (`#/##/###`, bullets, `**bold**`) is rendered by
  `lib/markdown.ts` + `components/Markdown.tsx` — dependency-free, and
  XSS-safe by construction (agent-supplied attribute values stay text nodes).

## Remote Config (stage 6)

The highest-stakes flow, built around **review before action**, not around a big
red Push button (`src/components/config/ComposeStepper.tsx`):

- **Propose delivers nothing.** Target (ad-hoc ids or a saved group — the
  server re-resolves it) → edit (one object, prefilled decoded from an
  agent's report) → review (the #100 pipeline verdict + a line diff against
  the agent's current config) → propose (immutable revisions). Approve is a
  separate step requiring the `approver` role; **creating the rollout IS the
  approval**, and it releases the canary only.
- **`<redacted>` is un-pushable in the UI too**: the editor refuses the
  sentinel and prefill refuses a redacted report — pushing one wipes fleet
  credentials, so it never gets as far as the server's 422.
- **Terminal states stay terminal**: a `FAILED`/auto-reverted revision is
  poisoned; the Revisions tab offers Reactivate (a dialog with an audited
  reason, validation re-runs first), never an auto-retry. A paused rollout
  shows its one reason (`RolloutMonitor`) and Resume is offered only for
  causes fixable on the agent — abort (operator) and roll back (approver, a
  NEW gated rollout) stay visibly different decisions.
- **Diff** (`src/lib/textDiff.ts`): LCS line diff, unit-tested; shown only
  when a single agent is in scope — a diff against a moving set is not a
  review.

Playwright: `e2e/remote-config.spec.ts` drives the whole stepper against the
stateful mock transport (the mock makes "create rollout = approval"); the
mock's rollout/approval handlers enforce the operator/approver role floors, so
the denial cases run here too. Screenshot: `tools/screenshot-remote-config.mjs`.

## Agents (stage 5)

The fleet views are agent-type-aware the same way the server is — nothing
assumes a collector that the server did not say:

- **Selection is server-side only** (#132): the one-box SQL search drives
  `GET /agents?q=...`, the health chips use the `healthy` parameter, and
  nothing is filtered in the browser. The query lives in the URL (`/?q=...`),
  so a detail page's back link re-enters the same selection and a filtered
  list is shareable. Columns come from `GET /agents/query/schema` (core
  columns plus the attributes this fleet actually reports) via the column
  picker — never hard-coded collector fields.
- **Last seen, in words, everywhere** (`lib/heartbeat.ts` + `lib/useNow.ts`):
  the heartbeat renders as an age — `just now`, `12s ago`, `6m ago`, a date
  past a week — in the Agents table (default column), on each chip of the
  Overview fleet graphic, and on the detail page, for live agents as well as
  quiet ones; the exact timestamp stays in the `title`. One clock per view
  (`useNow`, 10s) keeps the ages ticking instead of freezing at fetch time.
  It deliberately says *when*, not *whether*: staleness is the server's
  call (`is_stale()`: `max(180s, 3 × observed gap)`, shared with the reaper and
  the rollout gate), and the reaper removes a silent agent within ~15s of that,
  so a browser-side "stale" verdict would be a guess shown for a moment.
  For the fleet-wide answer use SQL instead: `stale = 1`.
- **Detail adapts to the type** (`/agents/$agentId`): identity/health are
  common; the telemetry panels and the component inventory render from the
  agent's profile (`GET /agent/types`, `server/profiles/data/*.yaml`), so a
  `generic` agent gets the honest "this profile does not report telemetry"
  fact instead of empty collector tiles.
- **Status vocabulary is the server's** (`src/lib/deliveryStatus.ts`):
  push `delivered|queued|unchanged`, assignment
  `none|queued|delivered|applied|failed|poisoned|unconfirmed`, apply verdict
  `UNSET|APPLYING|APPLIED|FAILED` — `StatusBadge` maps them to tokens; an
  unknown value renders muted, never a new color.
- **`<redacted>` is opaque**: masked config values render as-is with the
  masked paths listed; they are never stripped, edited, or echoed back on a
  save (editing arrives in stage 6).

Screenshots: `tools/screenshot-agents.mjs` regenerates the stage-5 captures
against the mock dev server (`VITE_USE_MOCK_API=1`, port 5199).

## Navigation (stage 4)

The sidebar mirrors `ui/app.py`'s `st.navigation` tree so every Streamlit page
stays reachable during the migration: Agents (`/`, default), Reports, Groups,
Remote Config, Help, plus an **Admin** section (Alerts, Compliance, Agent
tokens, Sessions, Updates, Release emails) with flat URLs like Streamlit's.
The Admin section and its admin-only items (Agent tokens, Sessions) are gated
client-side by the role ladder in `src/lib/roles.ts` — a hint only; the
server's `require_roles` is the authority. Pages not yet built render a
`StubPage`; unknown URLs land on the root `NotFound`; route render crashes hit
the `RootError` boundary.

## Regenerating the API types

`src/api/schema.d.ts` is generated from the live server's OpenAPI schema —
never hand-edit it. Start the server, then:

```bash
npm run gen-api
# or: OPENAPI_URL=http://other-host:4320/openapi.json npm run gen-api
```

The codegen runs in `tools/api-codegen/` (its own package.json pinning
TypeScript 5.x) because this project compiles with TypeScript 7 (native), which
openapi-typescript does not yet support at runtime.

## API layer conventions

- All HTTP goes through `src/api/client.ts` (openapi-fetch, bound to `/api`).
  Components never call `fetch`.
- 401/403/429/503 semantics are normalized in exactly one place,
  `src/api/errors.ts`, mirroring `ui/session.py::classify_response`:
  401 ⇒ drop the session and show login (`relogin`), 403 ⇒ keep the session
  (role refusal vs. read-only mode), 429 ⇒ lockout with retry-after,
  503 ⇒ audit-store refusal. Use `unwrap()` in `src/api/endpoints.ts`.
- Payloads the server types as `Dict[str, Any]` (schema says `unknown`) get a
  narrow interface in `src/api/endpoints.ts` mirroring the server
  (`AgentState.to_dict()`, `credential_summary()`, …).
- Mock mode: handlers + fixtures in `src/api/mock.ts` / `fixtures.ts`; Playwright
  and offline dev use `VITE_USE_MOCK_API=1`. The mock auth handlers are
  stateful (a flag stands in for the cookie) so the login → shell → logout
  round-trip is exercised without a server. The mock runs in **principals
  mode** with a name→role map (`fixtures.mockPrincipals`): sign in as
  `viewer`/`operator`/`approver`/`admin` (same password) and every handler
  enforces the same role floor the live endpoint does, so the 403 contract is
  testable end to end (`e2e/roles.spec.ts`).
- Design tokens live in `src/index.css` (`--color-*`); never hard-code colors in
  components.
