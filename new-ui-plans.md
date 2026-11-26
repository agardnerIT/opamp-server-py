# New UI — Plan for Staged Implementation

> **Status: complete.** Stages 1–9 built the console; **stage 10 (below) deleted
> Streamlit** and handed port 8501 over (2026-11-27). This document is the
> history of that migration plus the stage-11 backlog — the shipped UI is
> `web/` and its living documentation is `web/README.md`.

This document lets **independent agent sessions** implement the new OpAMP
human-facing UI one stage at a time, without seeing the conversations that
produced the plan. Each stage is self-contained: read stage 0, your stage,
and any stage marked `depends on` that is marked **done**.

Status vocabulary: **todo / in-progress / done**. Update the status line of
your stage when you finish it, and record anything the next stage needs to
know in that stage's "Notes for next stage".

---

## Stage 0 — Context and decisions (read this first)

### Why we are replacing Streamlit

The Streamlit UI (`ui/`, ~6,000 lines, 18 pages) served well, but its rerun
model forces awkward designs for exactly the pages humans use most:

- Admin tab statelessness (see the header comment in `ui/app.py`) — actions
  that rerun used to move the operator somewhere they never asked to go.
- Multi-step stateful flows (login, config-push approval, release management)
  fight the "whole script reruns" paradigm.
- The per-agent-type views degraded into a mess once the server supported
  more agent types than just the OTel collector.

Decision (made with the project owner, 2026-12): full redesign as a separate
frontend, **not** a 1:1 port. Pages migrate by pain order, not by category.
Streamlit stayed running and untouched until the new UI reached full parity
(it has since been deleted — see stage 10).

### Decided constraints (do not relitigate)

| Decision | Value | Rationale |
|---|---|---|
| Stack | React 19 + TypeScript (strict) + Vite + Tailwind v4 | Largest training corpus → lowest agent error rate; compiler = the reviewer (owner cannot review frontend code) |
| Components | shadcn/ui (Radix) | Accessible dialogs/forms/dropdowns for the flow-heavy pages; one `<Button>` prevents five invented styles |
| Server state | TanStack Query | App is ~90% server state; do not hand-roll caching/invalidation |
| Routing | TanStack Router | Already wired |
| Auth transport | `httpOnly` cookie sessions (new) | Browser-native, survives refresh; replaces Bearer-in-memory. Server keeps `Authorization` header support for API clients |
| Serving | Tiny nginx container: serves `web/dist` static files, proxies `/api` (and the OpAMP WS is *not* proxied — it is agent-facing) | Keeps the `./data` read-only-mount security posture between UI container and server container |
| API base | `/api` in dev (Vite proxy → `127.0.0.1:4320`), same origin behind nginx | One origin in every environment |
| Tests | Playwright (e2e + screenshots) | Replaces what `tests/test_ui_smoke.py` (Streamlit `AppTest`) does today |
| Working title | "OpAMP Console" | Cosmetic; owner has not objected |

### Rules every stage must follow

1. **Keep `web/` green**: `cd web && npm run build` (includes `tsc -b`) and
   `npm run test` must pass before your session ends. If you add Playwright
   tests, `npm run e2e` too.
2. **One dark theme**, tokens in `web/src/index.css` (`--color-*`). Never
   hard-code hex values in components; extend tokens if needed.
3. **All HTTP via the typed client** (`web/src/api/`), never raw `fetch` in
   components.
4. **401 vs 403 semantics are sacred** (they encode the server's contract,
   see `ui/session.py`): 401 ⇒ drop the session, show login; 403 ⇒ keep
   session, name the required role. Never conflate them.
5. **Never render or store a config body containing `<redacted>`** — pushing
   a redacted body back wipes fleet credentials. The sentinel means
   "keep stored value"; the UI must treat it as opaque.
6. **Respect the role ladder** (`viewer < operator < approver < admin`,
   `server/auth.py`). Hide/disable actions the current role cannot take, but
   the server is the authority — never trust client-side gating.
7. **Nothing proprietary** (standing decision #150): this UI speaks standard
   OpAMP concepts only, via the server's own REST API.
8. **Screenshots**: whenever a user-visible screen changes meaningfully,
   capture Playwright screenshots and add the best ones to the README per
   the "UI Changes" convention in `memory.md`.
9. **Session completion** (per root `AGENTS.md`): quality gates, then
   `git pull --rebase && git push`. Work is not done until pushed.
10. Do not modify `ui/` (Streamlit) except to delete pages at cut-over time.
    It must stay working for the whole migration.

---

## Stage 1 — Scaffold (status: **done**)

Depends on: nothing.

Exists in `web/`:

- Vite + React + TS scaffold with Tailwind v4 (`@tailwindcss/vite` plugin,
  `@import 'tailwindcss'` in `src/index.css`).
- TanStack Router (code-based, `src/router.tsx`) with routes:
  `/login`, `/` (Agents), `/groups`, `/remote-config`, `/reports`.
- `AppShell` with sidebar nav + active states + session-status slot.
- `Login` page: complete form UI, submit intentionally stubbed with
  "not wired yet" error.
- Dev proxy `/api` → `http://127.0.0.1:4320` in `vite.config.ts`.
- Design tokens in `src/index.css` (starting palette, dark-first).
- `web/.gitignore` covers `node_modules` and `dist`.

Notes for next stage: default Vite leftovers removed (`App.tsx`, `App.css`,
assets). Placeholder pages (`Groups`, `RemoteConfig`, `Reports`) are
single-`<h1>` files; Agents is the same but it is the default route.

---

## Stage 2 — Typed API client (status: **done**)

Depends on: stage 1.

Goal: a fully typed client for the server REST API, generated, not hand-written.

Tasks:

1. Add `openapi-typescript` (+ `openapi-fetch`) as dev deps.
2. Generate types from the live schema: `GET http://127.0.0.1:4320/openapi.json`
   (start the server per `startDevStack.sh` or `docs/api-reference.md`). Commit
   the generated file; document the regeneration command in `web/README.md`.
3. Create `web/src/api/client.ts`: an `openapi-fetch` instance bound to `/api`.
4. Create `web/src/api/` thin wrappers only where ergonomics demand (e.g.
   error normalization that distinguishes 401/403/429/503 like
   `ui/session.py::classify_response`).
5. Add a tiny fake/mock mode (MSW or a fetch stub) so Playwright tests and
   dev-without-server both work.

Done when: `npm run build` green; a demo page (Agents) renders real data from
the live server using the typed client; 401/403/429/503 normalized in one place.

Notes for next stage (written 2026-12-01, all gates green — build, vitest 14
passing, oxlint clean):

- Layout: `src/api/client.ts` (openapi-fetch, baseUrl `/api`),
  `schema.d.ts` (generated, 4.7k lines), `errors.ts` (ApiError +
  `classifyError` + `unwrap` — mirrors `ui/session.py::classify_response`,
  including the read-only 403 hint and Retry-After header/payload/fallback),
  `endpoints.ts` (thin wrappers + narrow payload interfaces like `AgentEntry`
  for the `Dict[str, Any]` responses the schema types as `unknown`),
  `mock.ts`/`fixtures.ts` (fetch stub via `VITE_USE_MOCK_API=1`).
- **TS7 gotcha**: the project compiles with TypeScript 7 (native) but
  openapi-typescript needs the TS 5 compiler API at runtime, so codegen runs in
  `web/tools/api-codegen/` (own package.json pinning TS 5.9). Use
  `npm run gen-api`, never `npx openapi-typescript` from `web/`. Deps for the
  toolchain had to be installed with `--legacy-peer-deps` for the same reason.
- `npm run test` now exists (vitest); stage rules require it to stay green.
- `tsconfig.app.json` now has `strict: true` (the decided constraint; stage 1
  shipped without it). Everything currently compiles under strict.
- Fixtures include two agent types (`otelcol` + pinned `generic`) so stage 5's
  type-aware views can be developed offline.
- oxlint treats any `use*` function as a React hook — avoid that prefix for
  plain helpers (cost one rename already).
- The demo Agents page renders `/agents` via TanStack Query with a real
  empty state; the live fleet was empty when this stage finished, so the
  populated-table view has only been seen against fixtures.
- **CORRECTION (2026-11-22, stage 3):** the claim above that the demo page
  rendered "real data from the live server" was wrong — the Vite proxy
  shorthand (`'/api': 'http://…'`) preserves the full path, so every proxied
  request hit `/<api>/…` on the server and answered FastAPI's 404
  `{"detail":"Not Found"}`. Fixed in `vite.config.ts` with a `rewrite:` that
  strips the `/api` prefix; **stage 10's nginx config must strip it the same
  way** (the server serves unprefixed paths).

---

## Stage 3 — Cookie-session auth end-to-end (status: **done**)

Depends on: stage 2. **Touches the FastAPI server** (`server/auth.py` and the
session routes) — that is expected and sanctioned.

### Backend half — DONE (2026-11-22, server-side session + tests)

Implemented in `server/auth.py` + `server/main.py`, tests in
`tests/test_auth.py::TestCookieSessions` (6 tests; full suite 1885 passing)
— **frontend half landed 2026-11-22, see "Notes for next stage" below**:

- `POST /auth/session` now also sets `Set-Cookie: opamp_admin_session=<token>`
  — httpOnly, SameSite=Strict, `max_age` = session TTL, path=/. Same token as
  the bearer body: one session row, two transports.
- Secure flag: `cookie_secure()` — `ADMIN_COOKIE_SECURE` env wins (true/false),
  else Secure iff the request arrived over https (direct or `X-Forwarded-Proto`
  when `TRUST_PROXY_HEADERS=true`). Plain-http localhost dev gets no Secure
  (otherwise the browser would drop it).
- `authenticate()` credential order: bearer header → cookie → Basic. ANY
  Authorization header (including Basic) means the cookie is never consulted,
  so `opampctl`/scripts are unaffected by browser cookies (this bit the test
  suite once — see TestSessions::test_list_and_revoke_a_session).
- `DELETE /auth/session` revokes the session AND clears the cookie (Max-Age=0).
- 401/403/429/503 semantics unchanged; read-only mode untouched.

Frontend still to do (the tasks below) — the cookie is live server-side, so
the Login page can start posting to `POST /api/auth/session` immediately.

Goal: replace Bearer-in-memory with `httpOnly` cookie sessions for the
browser, keeping Bearer for CLI/API clients.

Backend tasks:

1. `POST /auth/session` (and the extend/re-auth path) additionally sets
   `Set-Cookie`: `httpOnly; SameSite=Strict; Secure` (Secure when not
   localhost), with the existing session token value.
2. `require_roles` accepts the cookie as a credential source. Authorization
   header still wins if both present.
3. `DELETE /auth/session` clears the cookie.
4. Server tests for: cookie accepted, cookie revoked on logout, header still
   accepted, Secure-flag behavior toggled by config.

Frontend tasks:

1. Login page posts to `POST /api/auth/session`; on success, TanStack Query
   session/identity query invalidates and router redirects to `/`.
2. A `useSession` hook backed by the identity endpoint (role, expiry, name).
   AppShell session-status slot shows role + expiry; "Extend" re-auths via
   the audited re-auth path (`POST` new session then `DELETE` old — read the
   note in `memory.md` line ~131: the re-auth flag is *read, never popped*).
3. Global fetch wrapper behavior: on 401 ⇒ clear session state, redirect to
   `/login`; on 403 ⇒ toast naming the required role; 429/503 ⇒ specific
   messages. Keep token/password never persisted to `localStorage`.

Done when: login → shell → logout round-trip works against the live server;
role gating reflected in UI; Playwright test for login failure/success.

---

## Stage 4 — App shell + navigation parity (status: **done**, 2026-11-22)

### Notes for next stage (written 2026-11-22 when stage 3 closed)

Frontend auth is live in `web/`: `endpoints.ts` owns `login`/`logout`/
`extendSession`/`getMe`/`getAuthStatus` (the token in the 201 body is
**discarded** there — never returned, never persisted); `session.ts` holds
`useSession` (query key `['session']`), `useLogin`, `useLogout`,
`useExtendSession`. The **global 401 handler** registered in `main.tsx`
(`setUnauthorizedHandler`, `errors.ts`) drops session state and routes to
`/login` on any relogin-classified error from `unwrap()` — pages must not
hand-roll 401 handling. "Extend" is DELETE-then-POST (`extendSession` doc
comment explains why the cookie flow inverts the Streamlit ordering). Mock
mode is auth-stateful (flag for the cookie) with `fixtures.validPassword`;
Playwright is installed (`npm run e2e`, config `playwright.config.ts`,
mock-mode dev server on :5199) with the login round-trip in `e2e/auth.spec.ts`.
For stage 4's Admin-section role gating, use `useSession().data.role` —
`WhoAmI.role` + `roles` come from `GET /auth/me`. Screenshots:
`docs/screenshots/console-{agents-session,login-error}.png` (mock fleet).

Cache hygiene hardening (2026-11-22): login/re-auth now invalidate **all**
queries (`queryClient.invalidateQueries()`), logout and the global 401 handler
in `main.tsx` now `queryClient.clear()` — nothing fetched while signed out or
under a previous identity survives into the next session (role changes must
never reuse cached data). Also fixed vitest picking up Playwright specs:
`vitest.config.ts` now scopes `include` to `src/**/*.test.{ts,tsx}`
(previously `e2e/auth.spec.ts` failed the `npm run test` gate).

Depends on: stage 3.

**Landed 2026-11-22, all gates green (build, vitest 32, oxlint, Playwright 5).
Sidebar mirrors `ui/app.py` exactly: main nav in Streamlit order (Agents `/`,
Reports, Groups, Remote Config, Help) + Admin section with flat URLs
(`/alerts`, `/compliance`, `/agent-tokens`, `/sessions`, `/updates`,
`/release-emails`).** Implementation:

- `src/lib/nav.ts` owns `MAIN_NAV`/`ADMIN_NAV` + `visibleNav(nav, role)`;
  `src/lib/roles.ts` mirrors the server ladder (`roleAtLeast`) with unit tests
  in `nav.test.ts` (ladder, per-role visibility, Streamlit order parity).
- Admin visibility per the Streamlit pages' own gating: Alerts/Compliance/
  Updates/Release-emails = `operator`; Agent tokens/Sessions = `admin`;
  whole section hidden below `operator` and while the session query is
  pending (no low-role flash).
- Fixed a stage-1–3 bug on the way: the sidebar matched `/` with
  `fuzzy: true`, so Agents was marked active on every page; `/` now matches
  exactly (`aria-current="page"` asserted per-route in e2e).
- Unbuilt pages render `StubPage` (real title + which stage builds them);
  root route gained `notFoundComponent` (`NotFound.tsx`) and
  `errorComponent` (`RootError.tsx`, offers reset + link home).
- E2E: `e2e/nav.spec.ts` (admin section visible, all 11 routes reachable,
  active-state correctness, not-found round-trip). Screenshot:
  `docs/screenshots/console-nav-admin.png` via `tools/screenshot-shell.mjs`
  (starts the mock dev server itself, then captures after Admin nav + agent
  table are visible — do not shoot before the queries settle).

Done when: every Streamlit page has a corresponding route; build + tests green. ✅

### Notes for stage 5 (written 2026-11-22 when stage 4 closed)

- Route stubs to replace with real pages: `Help.tsx` (stage 7);
  `Alerts/Compliance/AgentTokens/Sessions/Updates/ReleaseEmails.tsx`
  (stage 8). `StubPage` takes `stage` so the placeholder names its builder.
- Reuse `visibleNav`/`roleAtLeast` (`src/lib/`) for any per-role action
  gating; keep it a hint — the server enforces.
- Sidebar section headers are plain `div` ("Admin", uppercase) between two
  `<nav aria-label>`s; stage 5's design pass can restyle but keep the two-nav
  structure (e2e queries `getByRole('navigation', { name: 'Admin' })`).

---

## Stage 5 — Agents list + agent detail, multi-type aware (status: **done**, 2026-11-22)

Depends on: stages 2–4. This is pain point #3 from the owner and the biggest
design task.

Goal: kill the "collector-shaped" assumptions. The UI must be
agent-type-aware the same way the server is (agent support = profile in
`server/profiles/data/*.yaml` + what the agent reports).

Tasks:

1. Agents list: server-side filtering/sorting/pagination (the API has
   `GET /agents?q=...` and `GET /agents/query/schema` — use them; do not
   filter client-side). Columns must derive from the query schema, not be
   hard-coded to collector fields.
2. Agent detail: sections that adapt to agent type — identity/health common
   to all; type-specific blocks (e.g. collector config/validation state,
   remote-config delivery state) rendered from the profile/reported data.
3. Delivery-state vocabulary exactly as the server defines it (memory.md
   ~109): push responses `delivered|queued|unchanged`; assignment states
   `none|queued|delivered|applied|failed|poisoned|unconfirmed`. Display
   them with consistent status colors; never invent states.
4. Hash mismatch handling: `last_remote_config_hash` is stored hex, wire is
   base64 — whatever the API exposes, render "applied/unconfirmed" only on
   real equality (the server already computes `applied`; trust it).
5. Design pass with the `better-interface` / `better-layout` skills; this
   page sets the visual language for everything after.

Done when: live server shows ≥2 agent types with type-appropriate detail
views; screenshots taken; Playwright covers list + one detail view.

Notes: this stage produces the reusable primitives (StatusBadge,
DataExplorer/SQL search surface — cf. `ui/sql_search.py`) later stages reuse.

### Notes for next stage (written 2026-11-22 when stage 5 closed)

All gates green: `npm run build` (tsc strict), vitest 52, oxlint, Playwright 11
(auth 2, nav 3, agents 6 in `e2e/agents.spec.ts`). Screenshots:
`docs/screenshots/console-agents-{list,agent-detail}.png` (mock fleet) via
`tools/screenshot-agents.mjs`. **Still to do against a live server** (stages 9
owns the live pass): connect ≥2 real agent types and confirm the type-aware
detail end-to-end — everything here was verified against fixtures that mirror
`AgentState.to_dict()` / `AgentProfile.describe()` byte-for-byte where it
matters (panels, telemetry, capability tags).

- **Where the primitives live** (reuse, do not re-invent):
  `src/lib/deliveryStatus.ts` (`statusTone`/`statusLabel` — the server's
  vocabulary, unit-tested), `src/components/StatusBadge.tsx`,
  `src/components/AgentTypeBadge.tsx` (profile display name/icon + source),
  `src/lib/sqlSuggestions.ts` (pure suggestion helper, unit-tested) +
  `src/components/SqlSearch.tsx` (combobox, Tab/Enter),
  `src/lib/configText.ts` (`configObjects` — display-only `configMap`
  decoder; `<redacted>` passes through opaque),
  `src/components/{Section,KeyValue,EmptyState}.tsx`.
- `AgentDetail`/`AgentEntry` narrow interfaces in `src/api/endpoints.ts`
  mirror `AgentState.to_dict()`; `capability_tags` are `{label, icon}` pairs
  (an earlier `string[]` was wrong). Mock handlers now do parameterized paths
  (`/agent/{id}`) and a minimal `q`/`healthy` selection stand-in — real SQL
  stays server-side.
- **Router gotchas** (cost two e2e failures): components under the shell
  layout route must use `useParams`/`useSearch({ strict: false })` — the
  root-relative match id (`from: "/agents/$agentId"`) throws "Could not find
  an active match". Both list and detail routes validate `q` in the search
  (`validateSearch`), so the SQL selection rides the URL (#146 parity).
- **Not done (deliberately deferred):** saved-group filter on the list
  (stage 7 owns Groups — the `group` param exists server-side); sorting/
  pagination (the API has no sort param — `q` can ORDER BY; the server caps
  selections at 10k rows); propose/edit config + compliance check buttons
  (stage 6/8 mutations). The detail page is display-only by design.
- Attribute columns are named exactly as agents report them (`host.arch`,
  quoted as `"host.arch"` in SQL). Don't "fix" that spelling in fixtures —
  the e2e suggestion test types `dep` and expects the quoted insert.

---

## Stage 6 — Remote Config + the push/release approval flow (status: **done**, 2026-11-22)

Depends on: stage 5 primitives.

Goal: fix pain point #1. This is the highest-stakes flow (config push =
fleet takeover, posture-gated).

Tasks:

1. Remote Config page: compose/offer config to groups or agents; consume the
   saved groups (#132) rather than ad-hoc selections.
2. Approval stepper: draft → review (diff against current) → approve
   (role `approver`) → push, with delivery + application state shown per the
   stage-5 vocabulary. Radix dialogs; every destructive step confirms.
3. Never render stored secrets unmasked: display goes through the same
   redaction contract as the API (`server/redaction.py` at the edge already
   handles it — the UI must not un-redact or echo `<redacted>` back on save).
4. Poisoned/FAILED/reverted configs are terminal: require explicit
   re-approval (memory.md ~73); the UI must not offer an "auto-retry".
5. Release management parity with the Streamlit Updates page (offers,
   manifests — `server/manifest.py`, `validate_manifest_version()`).

Done when: an offer→approve→push→apply round-trip is doable entirely in the
new UI against the live server; Playwright covers the stepper with a mocked
API; screenshots in README.

### Notes for next stage (written 2026-11-22 when stage 6 closed)

All gates green: `npm run build` (tsc strict), vitest 57, oxlint, Playwright 14
(3 new in `e2e/remote-config.spec.ts`). Screenshot:
`docs/screenshots/console-remote-config.png` via `tools/screenshot-remote-config.mjs`.
**Still to do against a live server** (stage 9 owns the live pass): the real
offer→approve→push→apply round-trip — everything here was verified against the
stateful mock transport.

- **Where the pieces live** (reuse, do not re-invent):
  `src/api/config.ts` (typed wrappers + narrow interfaces for posture, groups
  read-only, validate, propose/approve/reject/reactivate, rollouts — the
  generated schema types the defaulted `ConfigScope` booleans as required, so
  `proposeConfig` fills them), `src/components/config/ComposeStepper.tsx`
  (steps 1–5 + monitor), `src/components/config/RolloutMonitor.tsx` (batch
  progress, pause-reason help, abort/resume/rollback with confirm dialogs —
  reused by the Rollouts tab), `src/components/config/Diff.tsx` +
  `src/lib/textDiff.ts` (LCS line diff, unit-tested), `src/components/Modal.tsx`
  (native `<dialog>`: focus trap + Esc for free — the Radix/shadcn decision
  deferred until a page needs more than confirm dialogs; one less dep tree).
- **Role gating is client-side hints only** (`roleAtLeast` from
  `useSession().data.role`): Approve & roll out, Resume, Reactivate, Rollback
  need `approver`; Validate/Propose need `operator`. The server re-checks
  everything.
- **Scope coverage**: targeting supports ad-hoc agent-id selection and saved
  groups (`{agents}` / `{group}` scopes). The raw `filter` scope and
  `include_future` are NOT in the UI — deliberately (saved groups cover the
  selection-reuse case). Stage 7 (Groups) should not re-add targeting UI; it
  extends `ComposeStepper`'s scope builder only if the owner asks.
- **Scope-change 409 handling**: on a 409 from propose, the stepper reveals
  the `confirm_scope_change` checkbox with the server's own words — it is not
  shown preemptively.
- **Stage-6 task 5 ("release management parity with the Streamlit Updates
  page") did not match reality**: the Streamlit Updates page is only the
  update-check toggle/check (`GET /updates`, `PUT /updates/settings`,
  `POST /updates/check`) — there is no offers/manifest release API on the
  server (`server/manifest.py` is only the OCB manifest generator at
  `POST /agent/{id}/manifest`). That page therefore stays with stage 8
  (the `Updates.tsx` stub already says stage 8). Nothing release-shaped was
  dropped.
- **Gate seeding**: the dry-run plan seeds the gate input; the created rollout
  always sends the gate explicitly — the box is the decision, never the server
  default (same rule as the Streamlit page).
- Mock mode is now stateful for config: propose/reject/reactivate/rollout
  handlers mutate module state (`proposals`, `rolloutsState` in `mock.ts`).
  E2E must navigate by sidebar links after mock login — a full `page.goto`
  reloads the JS context and the session flag with it (cost 3 red tests).

---

## Stage 7 — Read pages: Reports, Groups, Help (status: **done**, 2026-12-01)

Depends on: stage 5 primitives.

Mostly tables and empty states; quick wins. Reuse the stage-5 table/search
primitives. Groups = saved SQL selections with create/edit/run; keep parity
with `ui/pages/3_Groups.py` behavior, including the query language guide
(`docs/sql-agent-queries.md`) linked from Help.

### Notes for next stage (written 2026-12-01 when stage 7 closed)

All gates green: `npm run build` (tsc strict), vitest 64, oxlint, Playwright 17
(3 new in `e2e/read-pages.spec.ts`). Screenshots: `docs/screenshots/console-{groups,reports}.png`
via `tools/screenshot-read-pages.mjs`. **Still to do against a live server**
(stage 9 owns the live pass): everything here ran against the stateful mock.

- **Where the pieces live**: `src/api/groups.ts` owns the FULL group CRUD
  (`listGroups`/`getGroup`/`createGroup`/`updateGroup`/`deleteGroup`/`previewGroup`
  + `GroupView`) — `config.ts`'s read-only duplicates were removed and
  `ComposeStepper` now imports from `groups.ts` (one implementation, one
  `['groups']` cache key). `src/api/reports.ts` wraps the three report
  endpoints; `lib/reportTypes.ts` (`offeredReports`) + `lib/markdown.ts` /
  `components/Markdown.tsx` (dependency-free report renderer, XSS-safe by
  construction — agent-supplied attribute values stay text nodes).
- **Reports parity notes**: offered reports are the union of the fleet's
  profiles' `reports:` (fleet always) — same #121 rule as Streamlit. The
  outdated report's reference version is a free-text semver input (default
  `0.149.0`), NOT a GitHub-releases dropdown — the Streamlit page fetched
  GitHub tags, which the new UI does not do (single-origin rule); owner may
  ask for it back. Download is Markdown-only (Streamlit's "Download CSV"
  downloaded the same markdown text — a quirk, not replicated).
- **Groups parity notes**: search/save/preview are viewer actions;
  save/edit/delete need `operator` (hinted only). The editor reuses `SqlSearch`
  remounted by key when a saved group's query is loaded (SqlSearch reads
  `value` only on mount). The saved query IS the editor text (no duplicate
  textarea). Legacy `definition`-kind groups load an empty editor and show
  the "no SQL query yet" note, as Streamlit did. Delete confirms via `Modal`
  and shows the server's message on a live-rollout 409.
- Mock mode is now stateful for groups (CRUD + preview mutate `groupsState`)
  and serves all three report endpoints. Group fixtures carry the full
  `GroupView` shape, including a legacy group with an unknown pin.
- Streamlit parity NOT ported (deliberate): the connect-snippet generators on
  Help (profile Python data, not exposed by the REST API — Help describes
  profiles from `GET /agent/types` instead) and the attribute-keys endpoint
  (`GET /groups/attribute-keys`, superseded by `/agents/query/schema`).

---

## Stage 8 — Admin pages (status: **done**, 2026-12-02)

Depends on: stage 3 (auth/roles).

Port parity for: Alerts (incl. webhook redaction round-trip rules —
`<redacted>` sentinel = keep stored value, memory.md ~125), Compliance,
Agent tokens (mint/revoke with confirm dialogs), Sessions (list/revoke),
Updates, Release emails. Each is small; batch them.

### Notes for next stage (written 2026-12-02 when stage 8 closed)

All gates green: `npm run build` (tsc strict), vitest 71 (7 new in
`mock.test.ts`), oxlint, Playwright 22 (5 new in `e2e/admin.spec.ts`).
Screenshots: `docs/screenshots/console-{alerts,agent-tokens,sessions,compliance,updates}.png`
via `tools/screenshot-admin.mjs`. Everything ran against the stateful mock;
stage 9 owns the live pass and the role-denial cases.

- **Where the pieces live**: `src/api/admin.ts` owns ALL six admin surfaces
  (alerts, agent tokens, admin sessions, compliance, updates) — typed payloads
  mirror the `dict`-returning handlers in `server/main.py`; components never
  import the generated schema.
- **Alert redaction is a round-trip, not a guard**: `webhook_url`/`headers`
  render the `***REDACTED***` sentinel verbatim and PUT passes it back
  unchanged (the alerts analogue of the config `<redacted>` rule; the sentinel
  IS the API contract here — `server/alerts.py merge_redacted`). "Send test"
  uses `/alerts/test`'s `event_config` escape hatch to validate unsaved
  settings — Streamlit's implicit autosave-then-test is gone.
- **Slimmed vs Streamlit, deliberately** (owner's "less is more" instruction):
  - Alerts: an event *selector* instead of tabs; one draft at a time.
  - Sessions: per-row Revoke button (Streamlit made the operator retype the
    12-char id — typo bait); confirm via the shared `Modal`.
  - Compliance: fleet-wide verdicts already live on the Agents list rows, so
    this page keeps only policy validation, the admin reload, and a single
    on-demand agent check (Streamlit's multiselect batch is gone).
  - Updates and **Release emails are one page now**: the signup form lives on
    the project's update Worker, which the REST API does not expose, and an
    iframe would have to hard-code an external URL (same single-origin rule
    stage 7 set for reports). The route `/release-emails` is gone — nav,
    `nav.test.ts` and `e2e/nav.spec.ts` updated with it.
- **Mock gotchas for the next session**: the mock session now survives full
  page navigations by mirroring the module flag into `localStorage`
  (`mock.ts::sessionActive/setSessionLive`) — Playwright `goto` reloads the
  module, and admin endpoints 401 while signed out, so this is load-bearing.
  The e2e/dev server for screenshots MUST be started with
  `VITE_USE_MOCK_API=1 npm run dev` — without it, requests hit the Vite proxy
  and login 401s against nothing.
- Role hints only: mint/revoke buttons disable below `admin` with a caption;
  the server remains the authority (stage 9 owns denial-case coverage).

---

## Stage 9 — Test + screenshot parity, README (status: **done**, 2026-11-22)

Depends on: stages 5–8.

1. Playwright suite covers: auth, agents list/detail, approval stepper,
   each admin mutation (with role-denial cases).
2. Screenshot set regenerated; README "latest and greatest" section switched
   to the new UI or shown side-by-side during transition.
3. Document local dev (server + `npm run dev`) and regeneration commands in
   `web/README.md`.

### Notes for stage 10 (written 2026-11-22 when stage 9 closed)

All gates green: `npm run build` (tsc strict), vitest 76, oxlint, Playwright 25
(3 new in `e2e/roles.spec.ts`) plus a **live smoke**, `npm run e2e:live`, that
boots a throwaway FastAPI server (temp `DATA_DIR`, throwaway password, config
push off, port **4330**) and walks the real cookie round-trip: login → shell →
privileged write (PUT /alerts) → logout → revoked cookie rejected.

- **Role-denial cases run against the mock, not just disabled buttons**: the
  mock now runs in *principals mode* — login name `viewer`/`operator`/
  `approver`/`admin` maps onto that role (`fixtures.mockPrincipals`, same
  password for all) and **every mock handler enforces the same role floor the
  live endpoint does** (`docs/api-reference.md` is the source). The 403 body
  is the server's exact wording (`role 'x' cannot perform this action
  (requires 'y')`) — keep it that way, `errors.ts` and the tests key off it.
  The mock's `authStatus` is now `auth_mode: "principals"`, so the login form
  shows the Name field; all specs and `tools/screenshot-*.mjs` fill it.
- **One real UX fix fell out** (not staged): TanStack Query's default retry
  re-fires 4xx three times, so a 403/404 took ~7s to render its message.
  `src/main.tsx` now retries only 5xx/network — 4xx answers are the server's
  decision. If a page ever needs retry-on-4xx, override it in that query,
  not globally.
- **Screenshots**: all 13 `docs/screenshots/console-*.png` regenerated via the
  five `web/tools/screenshot-*.mjs` scripts (commands documented in
  `web/README.md`); the README's "Latest screens" table is the current
  console. The live pass here was the auth/cookie smoke — fleet-dependent
  views (a real agent proposing → applying) still only exist against the
  mock; if the owner wants that proven live, it belongs with stage 10's
  compose stack, where an agent can actually connect.
- Stage 11's polish backlog is still open and unblocked.

---

## Stage 10 — Cut over, retire Streamlit (status: **done**, 2026-11-27)

Depends on: stage 9 sign-off by the owner. **Do not start without it.**

1. `docker/ui.nginx.conf` + `Dockerfile.ui` rewrite: nginx serving
   `web/dist` (multi-stage build) + `/api` proxy_pass to the server service.
   Keep `./data` mounted read-only into the *server* only.
2. Update `docker-compose*.yml` (ports, healthchecks).
3. Delete `ui/`, `requirements-ui.txt`, Streamlit tests, `.streamlit/`;
   update `tests/` and CI; remove Streamlit mentions from README/docs.
4. Keep `opampctl` / MCP / client layers untouched (they do not use the UI).

**Done (2026-11-27).** Notes, in the order they mattered:

- `docker/ui.nginx.conf` (new) + `Dockerfile.ui` (rewritten as node-build → nginx).
  The proxy is `location /api/ { proxy_pass http://server:4320/; }` — the trailing
  slash is the prefix strip, the same rule the vite dev proxy needed. `index.html` is
  `no-store`, `/assets/` immutable, every other path falls back to the shell.
- `docker-compose.yml`: `ui` publishes `127.0.0.1:8501:80` and has **no `env_file:`
  and no volume** any more — the console is static files plus a proxy, so it holds no
  configuration and never touches the data volume (#132's trap in another service).
- `docker-compose.dev.yml`: the `ui` build is the image (a static bundle cannot
  hot-reload), and the vite `console` profile moved to **8502** so it can never fight
  the built UI for 8501. Host `npm run dev` still prefers 8501.
- Deleted `ui/`, `.streamlit/`, `requirements-ui.txt`, the six Streamlit test modules,
  the agent-detail golden fixture and the JS SQL harness; `[ui]` extra removed.
- `web/package.json` dropped `openapi-typescript` (codegen lives in
  `tools/api-codegen` with its own TS 5 pin). It made `npm ci` fail `ERESOLVE`
  against typescript 7, which would have broken both the image build and CI.
- CI: new `console` job (`npm ci`, build, vitest, oxlint); `requirements-ui.txt`
  gone from the Python job.
- `tests/test_compose_ui.py` fences the hand-over: port pair, no config/volume in the
  container, prefix strip, SPA fallback, and "nothing still ships Streamlit".
- Docs: README (screenshots were Streamlit captures — replaced with
  `docs/screenshots/console-*.png` and the stale `assets/*.png` deleted),
  development/security/remote-config/release-emails, `CLAUDE.md`, `web/README.md`.
  Stage 11's backlog is untouched and still open.

---

## Stage 11 — Stage-8 polish backlog (status: **todo**)

Improvements found while building stage 8; none block stage 9.

1. **Alert secret UX**: the `***REDACTED***` sentinel renders inside the
   webhook/headers inputs. Honest, but easy to misread as a literal value —
   consider a masked-field with a "replace secret" affordance that sends the
   sentinel when untouched.
2. **Alert headers validation**: the headers field is free-text JSON; the
   server 422s on garbage. Validate client-side before save.
3. **Batch compliance re-check**: stage 8 dropped Streamlit's multiselect
   batch check; if the owner misses it, add a fleet re-check (loop or a bulk
   endpoint) rather than reintroducing the multiselect.
4. **Expose the email-subscribe URL via the API** (e.g. in `GET /updates`):
   would let the Updates page link to the real signup form instead of GitHub
   releases.
5. **`session_id` reuse**: `GET /auth/sessions` returns a 12-char hash prefix;
   if a later server change makes ids non-prefixes, the per-row Revoke flow
   needs no change — the prefix contract is the server's to keep.

## Open questions (ask the owner before deciding unilaterally)

- Real name/brand beyond the working title "OpAMP Console".
- Whether the login page should support username+password or stays
  password-only (current server model is password-only).
- Light mode: dark-only for v1?
- Where the UI should surface the audit log (#97) — read-only view in Admin?


## Bugfixes and Changes 1

- The homepage is the agents screen. Perhaps it should be a more generic landing page showing people the system
- The agents page search box that accepts SQL: When I type `S` for `SELECT` the dropdown immediately shows me `capabilities` instead of `SELECT`
- The agents homepage: When no agents are connected it should show me a "quickstart guide". I choose an agent type I want to connect, I can generate an API token and it gives me the snippet. Let's be friendly to newbie users and get them up and running quickly
- The reports page: As above, when no agents are connected I should see a quickstart
- The "remote config" page: Is this warning still true? "Config push is blocked by the security posture. Unmet: the server is not served over TLS: set PUBLIC_URL=https://<host> behind a TLS terminator, or TLS_TERMINATED_UPSTREAM=true once the proxy is in place (see docker/nginx.conf and docker/Caddyfile), config bodies would be stored unencrypted: set CONFIG_AT_REST_KEY to a base64-encoded 32-byte key (generate one with: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())") — or, for local development only, set OPAMP_ALLOW_PLAINTEXT_CONFIG_STORAGE=true and accept that any copy of the database exposes every exporter credential in the fleet (#98). Push stays refused until every requirement is met." I don't think it needs to be that prominent. Just an (i) modal or something or a "running in read only mode" would suffice. Plus I don't think it's even true. I'm sure I can push config the way it's running right now.
- The "remote config" page: I have a saved group "test-group-1" and I click "saved group" button and I get an error:
  Something went wrong
Cannot access 'selectedGroup' before initialization.
Try again
Back to Agents
- The UI should run on the original UI port of 8501
- The help page looks horrible and needs a redesign / relayout
- Alerts page: When I land on the alerts page, the "new agent" alert is prefilled. But when I try to check the "enabled" box it doesn't work. If I toggle to "agent disconnected" that enabled checkbox work. If I toggle back to "new agent" the "enabled" checkbox now works. Also, the checkboxes are tiny. Is there a standard UI framework or CSS library or something we can use?
- Alerts page: It MIGHT be clear to some users what each alert type is for, but one sentence saying "receive an alert when a new agent connects to this server" is so much more user friendly
- Is the UI mobile friendly? If not, make it so. We will NOT be designing a seperate mobile native app so this UI needs to work for both
- Compliance page: It says "OPA is not enabled
This server was started without an OPA connection, so there are no policies to validate and no checks to run." but doesn't point me anywhere (or give me an (i) hint) HOW I enable this or how I write policies or anything?
- This system is agent friendly: if a user has their own OpenAI compatible endpoint + token (eg. OpenAI, opencode, openrouter, Claude code etc.) could we give them the option to enter their details and have their AI drive this in a chat/agent native way? We can't provide an LLM but if they Bring Your Own Agent then we could?
- Sessions view: I see lots of expired sessions. That's great, but do they drop off or will they be there forever polluting the list? I also question the value of this since (I believe this app is currently only single user)?
- Releases: Release emails
Get one email when a new release is published — the address is stored by the project's signup service, not by this server, so it is not managed from here. Releases are listed at GitHub releases.  ... But it doesn't give me a box to sign up or anything?
- The "extend session" logic is uneccessary. Remove it and clean up the code. This is a one person app so sessions on 6hrs is OK.