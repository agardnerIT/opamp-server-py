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

  /* ---------------------------------------------------------------- */
  /* Config push (stage 6). Stateful enough to drive the stepper:       */
  /* propose stores a proposal, the dry run plans it, creating the      */
  /* rollout IS the approval, and abort/resume/rollback mutate it.      */
  /* ---------------------------------------------------------------- */
  {
    method: "GET",
    path: "/config/posture",
    respond: () => ({ body: fixtures.configPosture }),
  },
  {
    method: "GET",
    path: "/groups",
    respond: () => ({ body: fixtures.groups }),
  },
  {
    method: "POST",
    path: "/config/validate",
    respond: async (request) => {
      const body = (await request.json()) as { config_map?: Record<string, { body?: string }> };
      const bodies = Object.values(body.config_map ?? {}).map((o) => o.body ?? "");
      const redacted = bodies.some((b) => b.includes("<redacted>"));
      if (redacted) {
        return {
          body: {
            valid: false,
            errors: ["a config body contains '<redacted>' — pushing it would wipe stored credentials"],
            details: [],
            warnings: [],
            policy: { status: "skipped", violations: [] },
            config_hash: null,
            size_bytes: null,
            objects: null,
            profile: null,
            profile_source: null,
            structure_checked: true,
          },
        };
      }
      const size = bodies.reduce((n, b) => n + b.length, 0);
      return {
        body: {
          valid: true,
          errors: [],
          details: [],
          warnings: [],
          policy: { status: "skipped", violations: [] },
          config_hash: "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
          size_bytes: size,
          objects: bodies.length,
          profile: "otelcol",
          profile_source: "sniffed",
          structure_checked: true,
        },
      };
    },
  },
  {
    method: "POST",
    path: "/config/revisions",
    respond: async (request) => {
      const body = (await request.json()) as {
        scope?: { agents?: string[]; group?: string };
        note?: string | null;
      };
      const target = body.scope?.agents ?? (body.scope?.group ? groupMembers(body.scope.group) : []);
      if (target.length === 0) {
        return { body: { detail: "the scope resolved to no agents" }, status: 422 };
      }
      const proposal: MockProposal = {
        proposal_id: `prop-mock-${nextId.proposal++}`,
        status: "proposed",
        config_hash: "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        agents: target.length,
        revisions: target.map((agent_id) => ({ agent_id, revision_id: `rev-${agent_id.slice(0, 4)}-${nextId.revision++}` })),
        note: body.note ?? null,
        created_at: new Date().toISOString(),
      };
      proposals.set(proposal.proposal_id, proposal);
      fixtureRevisions.push(
        ...proposal.revisions.map((r) => ({
          revision_id: r.revision_id,
          agent_id: r.agent_id,
          proposal_id: proposal.proposal_id,
          status: "proposed",
          config_hash: proposal.config_hash,
          author: "admin",
          created_at: proposal.created_at,
          approved_by: null,
          approved_at: null,
          note: proposal.note,
          parent_revision_id: null,
          withdrawn: false,
          failed_at: null,
          failure_detail: null,
          verdict: { valid: true, structure_checked: true, profile: "otelcol" },
          group_id: null,
          group_name: null,
        })),
      );
      return { body: proposal, status: 200 };
    },
  },
  {
    method: "GET",
    path: "/config/revisions",
    respond: () => ({
      body: {
        revisions: [...fixtureRevisions].reverse(),
        count: fixtureRevisions.length,
        statuses: ["proposed", "active", "superseded", "rolled_back", "poisoned", "rejected"],
      },
    }),
  },
  {
    method: "GET",
    path: (path) => {
      const m = /^\/config\/revisions\/([a-z0-9-]+)$/.exec(path);
      return m ? { proposal_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const proposal: MockProposal | undefined = proposals.get(params.proposal_id);
      if (!proposal) return { body: { detail: "proposal not found" }, status: 404 };
      return {
        body: {
          ...proposal,
          statuses: [proposal.status],
          revisions: proposal.revisions.map((r) => ({
            ...r,
            status: proposal.status,
            withdrawn: false,
            failed_at: null,
            failure_detail: null,
            verdict: { valid: true, structure_checked: true, profile: "otelcol" },
            delivery: { state: null, delivered_at: null, channel: null, attempts: 0, poisoned: false, unconfirmed: false, held: false, last_error: null, current: false },
          })),
        },
      };
    },
  },
  {
    method: "POST",
    path: (path) => {
      const m = /^\/config\/revisions\/([a-z0-9-]+)\/(reject|reactivate)$/.exec(path);
      return m ? { proposal_id: m[1], action: m[2] } : null;
    },
    respond: async (_request, params) => {
      const proposal: MockProposal | undefined = proposals.get(params.proposal_id);
      if (!proposal) return { body: { detail: "proposal not found" }, status: 404 };
      if (params.action === "reject" && proposal.status !== "proposed") {
        return { body: { detail: `only proposed revisions can be rejected (this one is ${proposal.status})` }, status: 409 };
      }
      proposal.status = params.action === "reject" ? "rejected" : "proposed";
      for (const revision of fixtureRevisions) {
        if (revision.proposal_id === proposal.proposal_id) revision.status = proposal.status;
      }
      return { body: { ...proposal } };
    },
  },
  {
    method: "POST",
    path: "/config/rollouts",
    respond: async (request) => {
      const url = new URL(request.url);
      const proposalId = url.searchParams.get("proposal_id") ?? "";
      const proposal = proposals.get(proposalId);
      if (!proposal) return { body: { detail: "proposal not found" }, status: 404 };
      const body = (await request.json()) as {
        dry_run?: boolean;
        health_gate_seconds?: number | null;
        restart_after_apply?: boolean;
        note?: string | null;
      };
      const gate = body.health_gate_seconds ?? 90;
      const agentIds = (proposal.revisions ?? []).map((r: { agent_id: string }) => r.agent_id);
      const half = Math.max(1, Math.ceil(agentIds.length / 2));
      const batches = [
        { batch: 0, agents: half, agent_ids: agentIds.slice(0, half) },
        { batch: 1, agents: agentIds.length - half, agent_ids: agentIds.slice(half) },
      ].filter((b) => b.agents > 0);
      if (body.dry_run) {
        return {
          body: {
            dry_run: true,
            proposal_id: proposalId,
            config_hash: proposal.config_hash,
            status: "planned",
            agents: agentIds.length,
            batch: {
              count: batches.length,
              canary_percent: 10,
              max_agents_per_batch: null,
              max_percent_per_batch: null,
              size: half,
              health_gate_seconds: gate,
              ttl_minutes: 60,
              restart_after_apply: body.restart_after_apply ?? false,
            },
            batches,
            verdict: null,
          },
        };
      }
      if (proposal.status !== "proposed") {
        return { body: { detail: { message: `proposal ${proposalId} is already ${proposal.status}` } }, status: 409 };
      }
      proposal.status = "active";
      for (const revision of fixtureRevisions) {
        if (revision.proposal_id === proposalId) revision.status = "active";
      }
      const rollout: MockRollout = {
        rollout_id: `roll-mock-${nextId.rollout++}`,
        proposal_id: proposalId,
        kind: "rollout",
        status: "running",
        created_at: new Date().toISOString(),
        expires_at: null,
        completed_at: null,
        paused_reason: null,
        pause_detail: null,
        approver: "admin",
        note: body.note ?? null,
        config_hash: proposal.config_hash,
        batch: {
          current: 0,
          count: batches.length,
          size: half,
          canary_percent: 10,
          health_gate_seconds: gate,
          restart_after_apply: body.restart_after_apply ?? false,
          started_at: new Date().toISOString(),
        },
        agents: { total: agentIds.length, states: { delivered: batches[0].agents, pending: agentIds.length - batches[0].agents } },
        batches: [
          { ...batches[0], status: "in_flight", states: { delivered: batches[0].agents } },
          ...batches.slice(1).map((b) => ({ ...b, status: "pending", states: { pending: b.agents } })),
        ],
      };
      rolloutsState.push(rollout);
      return { body: rollout };
    },
  },
  {
    method: "GET",
    path: "/config/rollouts",
    respond: () => ({
      body: {
        rollouts: [...rolloutsState].reverse().map((r) => {
          const { batches: _omit, ...rest } = r;
          return rest;
        }),
        count: rolloutsState.length,
        statuses: ["pending", "running", "paused", "completed", "aborted", "cancelled"],
      },
    }),
  },
  {
    method: "GET",
    path: (path) => {
      const m = /^\/config\/rollouts\/([a-z0-9-]+)$/.exec(path);
      return m ? { rollout_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const rollout: MockRollout | undefined = rolloutsState.find((r) => r.rollout_id === params.rollout_id);
      return rollout ? { body: rollout } : { body: { detail: "rollout not found" }, status: 404 };
    },
  },
  {
    method: "POST",
    path: (path) => {
      const m = /^\/config\/rollouts\/([a-z0-9-]+)\/(abort|resume|rollback)$/.exec(path);
      return m ? { rollout_id: m[1], action: m[2] } : null;
    },
    respond: async (_request, params) => {
      const rollout = rolloutsState.find((r) => r.rollout_id === params.rollout_id);
      if (!rollout) return { body: { detail: "rollout not found" }, status: 404 };
      if (params.action === "abort") {
        if (rollout.status !== "running" && rollout.status !== "paused") {
          return { body: { detail: `a ${rollout.status} rollout cannot be aborted` }, status: 409 };
        }
        rollout.status = "aborted";
        return { body: { ...rollout } };
      }
      if (params.action === "resume") {
        if (rollout.status !== "paused") {
          return { body: { detail: `rollout is ${rollout.status}, not paused` }, status: 409 };
        }
        rollout.status = "running";
        return { body: { ...rollout } };
      }
      // rollback: a new gated rollout for the applied agents.
      const rollback: MockRollout = {
        ...rollout,
        rollout_id: `roll-mock-${nextId.rollout++}`,
        kind: "rollback",
        status: "running",
        created_at: new Date().toISOString(),
        paused_reason: null,
        pause_detail: null,
        proposal_id: rollout.proposal_id,
      };
      rolloutsState.push(rollback);
      return { body: rollback };
    },
  },
];

/** Mutable config-flow state (module scope, like the session flag). */
const nextId = { proposal: 1, revision: 1, rollout: 1 };
const proposals = new Map<string, MockProposal>();
const fixtureRevisions: Record<string, unknown>[] = [...fixtures.revisions];
const rolloutsState: MockRollout[] = [...(fixtures.rollouts as MockRollout[])];


/** Minimal shapes for the stateful config handlers (structural, not exact). */
interface MockRevision {
  agent_id: string;
  revision_id: string;
}
interface MockProposal {
  proposal_id: string;
  status: string;
  config_hash: string;
  agents: number;
  revisions: MockRevision[];
  note: string | null;
  created_at: string;
}
interface MockRollout {
  rollout_id: string;
  proposal_id: string | null;
  kind: string;
  status: string;
  created_at: string;
  paused_reason: string | null;
  [key: string]: unknown;
}

function groupMembers(name: string): string[] {
  const group = fixtures.groups.groups.find((g) => g.name === name);
  return group ? group.members : [];
}

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
