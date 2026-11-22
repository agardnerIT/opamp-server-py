# New UI — Plan for Staged Implementation

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
Streamlit stays running and untouched until the new UI reaches full parity.

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

## Stage 4 — App shell + navigation parity (status: **todo**)

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

Goal: the shell covers everything Streamlit's sidebar had, so no page is
unreachable during migration.

Tasks:

1. Extend nav to full parity: Agents, Reports, Groups, Remote Config, Help +
   Admin section (Alerts, Compliance, Agent tokens, Sessions, Updates,
   Release emails) — mirror `ui/app.py`'s `st.navigation` tree.
2. Routes for each; every route renders a stub page with the real page title.
3. Admin section visible only to roles that can use it (client-side hint
   only; server still enforces).
4. Empty/broken states: a `NotFound` route, and an error boundary.

Done when: every Streamlit page has a corresponding route; build + tests green.

---

## Stage 5 — Agents list + agent detail, multi-type aware (status: **todo**)

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

---

## Stage 6 — Remote Config + the push/release approval flow (status: **todo**)

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

---

## Stage 7 — Read pages: Reports, Groups, Help (status: **todo**)

Depends on: stage 5 primitives.

Mostly tables and empty states; quick wins. Reuse the stage-5 table/search
primitives. Groups = saved SQL selections with create/edit/run; keep parity
with `ui/pages/3_Groups.py` behavior, including the query language guide
(`docs/sql-agent-queries.md`) linked from Help.

---

## Stage 8 — Admin pages (status: **todo**)

Depends on: stage 3 (auth/roles).

Port parity for: Alerts (incl. webhook redaction round-trip rules —
`<redacted>` sentinel = keep stored value, memory.md ~125), Compliance,
Agent tokens (mint/revoke with confirm dialogs), Sessions (list/revoke),
Updates, Release emails. Each is small; batch them.

---

## Stage 9 — Test + screenshot parity, README (status: **todo**)

Depends on: stages 5–8.

1. Playwright suite covers: auth, agents list/detail, approval stepper,
   each admin mutation (with role-denial cases).
2. Screenshot set regenerated; README "latest and greatest" section switched
   to the new UI or shown side-by-side during transition.
3. Document local dev (server + `npm run dev`) and regeneration commands in
   `web/README.md`.

---

## Stage 10 — Cut over, retire Streamlit (status: **todo**)

Depends on: stage 9 sign-off by the owner. **Do not start without it.**

1. `docker/ui.nginx.conf` + `Dockerfile.ui` rewrite: nginx serving
   `web/dist` (multi-stage build) + `/api` proxy_pass to the server service.
   Keep `./data` mounted read-only into the *server* only.
2. Update `docker-compose*.yml` (ports, healthchecks).
3. Delete `ui/`, `requirements-ui.txt`, Streamlit tests, `.streamlit/`;
   update `tests/` and CI; remove Streamlit mentions from README/docs.
4. Keep `opampctl` / MCP / client layers untouched (they do not use the UI).

---

## Open questions (ask the owner before deciding unilaterally)

- Real name/brand beyond the working title "OpAMP Console".
- Whether the login page should support username+password or stays
  password-only (current server model is password-only).
- Light mode: dark-only for v1?
- Where the UI should surface the audit log (#97) — read-only view in Admin?
