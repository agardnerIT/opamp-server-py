# OpAMP Console — frontend

React 19 + TypeScript (strict) + Vite + Tailwind v4 + shadcn/ui (Radix) +
TanStack Query/Router. See `../new-ui-plans.md` for the staged plan; this UI
replaces the Streamlit UI (`ui/`) page by page until full parity.

## Development

```bash
npm install
npm run dev            # http://localhost:5173, proxies /api -> 127.0.0.1:4320
```

The FastAPI server must be running for live data (see `../startDevStack.sh`).
Without it, run against the built-in fixtures:

```bash
VITE_USE_MOCK_API=1 npm run dev
```

## Quality gates

```bash
npm run build   # tsc -b (strict) + vite build
npm run test    # vitest
npm run lint    # oxlint
```

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
  (later stages) and offline dev use `VITE_USE_MOCK_API=1`.
- Design tokens live in `src/index.css` (`--color-*`); never hard-code colors in
  components.
