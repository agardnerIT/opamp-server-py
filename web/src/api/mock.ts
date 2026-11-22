/**
 * Tiny fake-mode: a fetch stub for `api` (see client.ts) so the UI can run and
 * be Playwright-tested without a live server.
 *
 * Enabled with `VITE_USE_MOCK_API=1` at dev/build time. Fixtures live in
 * `fixtures.ts`; handlers below cover the endpoints the demo pages read. New
 * endpoints get a handler when a page starts reading them.
 */

import { fixtures } from "./fixtures";
import type { AgentEntry } from "./endpoints";

export function mockApiEnabled(): boolean {
  return import.meta.env.VITE_USE_MOCK_API === "1";
}

type Handler = {
  method: string;
  /**
   * Path after the `/api` base, without a trailing slash. A function receives
   * the parsed path and returns match captures (stage 5: `/agent/{id}`).
   */
  path: string | ((path: string) => Record<string, string> | null);
  respond: (
    request: Request,
    params: Record<string, string>,
  ) => { body: unknown; status?: number } | Promise<{ body: unknown; status?: number }>;
};

/**
 * Minimal stand-in for `agent_query.run()` (#132): the two WHERE shapes the
 * e2e suite and dev-without-server use. Anything else runs unfiltered — mock
 * mode demos selection, it does not re-implement SQLite.
 */
function runSelection(query: string, agents: AgentEntry[]): AgentEntry[] {
  const healthy = /healthy\s*=\s*(1|0)/i.exec(query);
  const type = /(?:type|agent_type)\s*=\s*'([^']+)'/i.exec(query);
  let selected = agents;
  if (healthy) {
    const wantHealthy = healthy[1] === "1";
    selected = selected.filter((a) => (a.healthy === true) === wantHealthy);
  }
  if (type) {
    const want = type[1].toLowerCase();
    selected = selected.filter((a) => a.agent_type.toLowerCase() === want);
  }
  return selected;
}

const HANDLERS: Handler[] = [
  {
    method: "GET",
    path: "/agents",
    respond: (request) => {
      const url = new URL(request.url);
      const q = url.searchParams.get("q");
      // `q` wins over the ad-hoc parameters on the server, exactly as here;
      // `group` is stage 7 territory (saved groups live on the Groups page).
      if (q) {
        const selected = runSelection(q, fixtures.agents.agents as AgentEntry[]);
        return {
          body: {
            agents: selected,
            count: selected.length,
            filters: {},
            query: q,
          },
        };
      }
      // Ad-hoc parameters: `healthy` is the one the list offers as chips.
      const healthy = url.searchParams.get("healthy");
      if (healthy === "true" || healthy === "false") {
        const want = healthy === "true";
        const selected = (fixtures.agents.agents as AgentEntry[]).filter(
          (a) => (a.healthy === true) === want,
        );
        return {
          body: {
            agents: selected,
            count: selected.length,
            filters: { healthy },
          },
        };
      }
      return { body: fixtures.agents };
    },
  },
  {
    method: "GET",
    path: "/agent/types",
    respond: () => ({ body: fixtures.agentTypes }),
  },
  {
    method: "GET",
    // `/agent/{agent_id}` — captures must not swallow `/agent/types`, which is
    // declared first for the same reason the server declares it first.
    path: (path) => {
      const m = /^\/agent\/([0-9a-f][0-9a-f-]+)$/i.exec(path);
      return m ? { agent_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const detail = fixtures.agentDetails[params.agent_id];
      return detail
        ? { body: detail }
        : { body: { detail: "Agent not found" }, status: 404 };
    },
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
  const handler = HANDLERS.find((h) => h.method === request.method &&
    (typeof h.path === "string"
      ? h.path === path
      : h.path(path) !== null));
  await new Promise((resolve) => setTimeout(resolve, LATENCY_MS));
  if (!handler) {
    return Response.json(
      { detail: `mock mode has no handler for ${request.method} ${path}` },
      { status: 404 },
    );
  }
  const params = typeof handler.path === "function" ? (handler.path(path) ?? {}) : {};
  // Handlers may be async (the login handler inspects the body).
  const { body, status = 200 } = await handler.respond(request, params);
  return Response.json(body, { status });
};
