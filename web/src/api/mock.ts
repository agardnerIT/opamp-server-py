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
  respond: (request: Request) => { body: unknown; status?: number };
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
];

const LATENCY_MS = 50;

/** fetch replacement bound to the same contract openapi-fetch expects. */
export const mockFetch: typeof fetch = async (input, init) => {
  const request = new Request(input, init);
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
  const { body, status = 200 } = handler.respond(request);
  return Response.json(body, { status });
};
