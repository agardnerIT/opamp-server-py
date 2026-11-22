/**
 * Tiny fake-mode: a fetch stub for `api` (see client.ts) so the UI can run and
 * be Playwright-tested without a live server.
 *
 * Enabled with `VITE_USE_MOCK_API=1` at dev/build time. Fixtures live in
 * `fixtures.ts`; handlers below cover the endpoints the demo pages read. New
 * endpoints get a handler when a page starts reading them.
 */

import { fixtures } from "./fixtures";

export function mockApiEnabled(): boolean {
  return import.meta.env.VITE_USE_MOCK_API === "1";
}

type Handler = {
  method: string;
  /** Path after the `/api` base, without a trailing slash. */
  path: string;
  respond: (
    request: Request,
  ) => { body: unknown; status?: number } | Promise<{ body: unknown; status?: number }>;
};

const HANDLERS: Handler[] = [
  {
    method: "GET",
    path: "/agents",
    respond: () => ({ body: fixtures.agents }),
  },
  {
    method: "GET",
    path: "/agents/query/schema",
    respond: () => ({ body: fixtures.querySchema }),
  },
  {
    method: "GET",
    path: "/auth/status",
    respond: () => ({ body: fixtures.authStatus }),
  },
  {
    method: "GET",
    path: "/auth/me",
    respond: () =>
      sessionLive
        ? { body: fixtures.me }
        : { body: UNAUTHORIZED, status: 401 },
  },
  {
    method: "POST",
    path: "/auth/session",
    respond: async (request) => {
      // The handler never inspects the password beyond accept/reject — same
      // contract as the server: 401 on bad credentials, no such-name hint.
      try {
        const body = (await request.json()) as { password?: unknown };
        if (body.password === fixtures.validPassword) {
          sessionLive = true;
          return { body: fixtures.session, status: 201 };
        }
        return { body: { detail: "invalid credentials" }, status: 401 };
      } catch {
        return { body: { detail: "invalid request body" }, status: 422 };
      }
    },
  },
  {
    method: "DELETE",
    path: "/auth/session",
    respond: () => {
      const was = sessionLive;
      sessionLive = false;
      return { body: { logged_out: was } };
    },
  },
];

/**
 * Auth-stateful handlers. The browser's real credential is the httpOnly cookie
 * the server sets; here a module-level flag stands in for it so the login →
 * shell → logout round-trip is exercisable without a live server (and by
 * Playwright, which never sees a real cookie jar against mock mode).
 */
let sessionLive = false;

const LATENCY_MS = 50;
const UNAUTHORIZED = { detail: "session expired or missing" } as const;

/** fetch replacement bound to the same contract openapi-fetch expects. */
export const mockFetch: typeof fetch = async (input, init) => {
  // Node's fetch (vitest) rejects relative URLs, browsers accept them — pin a
  // base so the same relative `/api` paths work in both. The request never
  // touches the network: only its parsed path is used for dispatch.
  let request: Request;
  if (input instanceof Request) {
    // openapi-fetch passes a fully-built Request — keep its method/body.
    request = init ? new Request(input, init) : input;
  } else {
    const rawUrl =
      typeof input === "string" ? input : input instanceof URL ? input.href : String(input);
    request = new Request(new URL(rawUrl, "http://mock.local"), init);
  }
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "").replace(/^\/api/, "") || "/";
  const handler = HANDLERS.find(
    (h) => h.method === request.method && h.path === path,
  );
  await new Promise((resolve) => setTimeout(resolve, LATENCY_MS));
  if (!handler) {
    return Response.json(
      { detail: `mock mode has no handler for ${request.method} ${path}` },
      { status: 404 },
    );
  }
  // Handlers may be async (the login handler inspects the body).
  const { body, status = 200 } = await handler.respond(request);
  return Response.json(body, { status });
};
