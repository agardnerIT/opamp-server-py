/**
 * Tiny fake-mode: a fetch stub for `api` (see client.ts) so the UI can run and
 * be Playwright-tested without a live server.
 *
 * Enabled with `VITE_USE_MOCK_API=1` at dev/build time. Fixtures live in
 * `fixtures.ts`; handlers below cover the endpoints the demo pages read. New
 * endpoints get a handler when a page starts reading them.
 */

import { fixtures } from "./fixtures";
import { attrValue, type AgentEntry } from "./endpoints";
import type { GroupView } from "./groups";

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
      // Zero-agent variant (see `MOCK_EMPTY_FLEET_KEY`): the quickstart screens.
      if (emptyFleet()) {
        return { body: { agents: [], count: 0, filters: {} } };
      }
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
      sessionActive() ? { body: meBody() } : { body: UNAUTHORIZED, status: 401 },
  },
  {
    method: "POST",
    path: "/auth/session",
    respond: async (request) => {
      // Principals mode (fixtures.authStatus): the name must match one, and
      // the name maps onto the role a principal record carries — same
      // contract as the server: 401 on bad credentials, no such-name hint.
      try {
        const body = (await request.json()) as { name?: unknown; password?: unknown };
        const name = typeof body.name === "string" ? body.name.trim().toLowerCase() : "";
        const role = fixtures.mockPrincipals[name] ?? (name === "admin" ? fixtures.defaultRole : null);
        if (body.password === fixtures.validPassword && role !== null) {
          setSessionLive(true, role);
          return {
            body: {
              ...fixtures.session,
              principal: { name: role === "admin" ? "admin" : role, role },
            },
            status: 201,
          };
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
      const was = sessionActive();
      setSessionLive(false);
      return { body: { logged_out: was } };
    },
  },

  /* ---------------------------------------------------------------- */
  /* Reports (stage 7). The markdown fixtures mirror `server/reports.py` */
  /* for this fleet; parameters are echoed, not honoured.                */
  /* ---------------------------------------------------------------- */
  {
    method: "GET",
    path: "/reports/fleet",
    respond: () => ({ body: fixtures.reports.fleet }),
  },
  {
    method: "GET",
    path: "/reports/heavy-collectors",
    respond: (request) => {
      const threshold = new URL(request.url).searchParams.get("threshold");
      if (threshold !== null && !/^(0(\.\d+)?|1(\.0+)?)$/.test(threshold)) {
        return { body: { detail: "threshold must be within [0, 1]" }, status: 422 };
      }
      return { body: fixtures.reports.heavy };
    },
  },
  {
    method: "GET",
    path: "/reports/outdated-collectors",
    respond: (request) => {
      const version = new URL(request.url).searchParams.get("version");
      if (version !== null && !/^\d+\.\d+\.\d+$/.test(version)) {
        return { body: { detail: "version must be plain semver, e.g. '0.149.0'" }, status: 422 };
      }
      return { body: { ...fixtures.reports.outdated, version: version ?? "0.149.0" } };
    },
  },

  /* ---------------------------------------------------------------- */
  /* Groups (stage 7): stateful CRUD + preview. Members resolve like    */
  /* the server does on read — SQL via runSelection, legacy definitions */
  /* by deployment.environment, plus pins that match a live agent.      */
  /* ---------------------------------------------------------------- */
  {
    method: "GET",
    path: "/groups",
    respond: () => ({ body: { groups: groupsState, count: groupsState.length } }),
  },
  {
    method: "POST",
    path: "/groups",
    respond: async (request) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const body = (await request.json()) as {
        name?: string;
        query?: string | null;
        definition?: Record<string, unknown> | null;
        include?: string[];
        exclude?: string[];
        description?: string | null;
        owner?: string | null;
      };
      const name = (body.name ?? "").trim();
      if (!/^[a-z0-9._-]+$/.test(name)) {
        return { body: { detail: "group name must be lowercase a-z, 0-9, '.', '_' and '-'" }, status: 422 };
      }
      if (groupsState.some((g) => g.name.toLowerCase() === name.toLowerCase())) {
        return { body: { detail: `group name '${name}' is already taken (names are unique case-insensitively)` }, status: 409 };
      }
      if (!body.query && !body.definition) {
        return { body: { detail: "exactly one selector is required: query or definition" }, status: 422 };
      }
      const group = mockGroup(name, body.query ?? null, body.definition ?? null, body.include ?? [], body.exclude ?? [], body.description ?? null, body.owner ?? null);
      groupsState.push(group);
      return { body: group };
    },
  },
  {
    method: "GET",
    path: (path) => {
      const m = /^\/groups\/([^/]+)$/.exec(path);
      return m ? { group_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const group = groupsState.find((g) => g.group_id === params.group_id);
      return group ? { body: group } : { body: { detail: "group not found" }, status: 404 };
    },
  },
  {
    method: "PUT",
    path: (path) => {
      const m = /^\/groups\/([^/]+)$/.exec(path);
      return m ? { group_id: m[1] } : null;
    },
    respond: async (request, params) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const group = groupsState.find((g) => g.group_id === params.group_id);
      if (!group) return { body: { detail: "group not found" }, status: 404 };
      const body = (await request.json()) as {
        query?: string | null;
        description?: string | null;
        owner?: string | null;
      };
      if (body.query !== undefined && body.query !== null) {
        group.query = body.query;
        group.definition = null;
        group.selector_kind = "query";
      }
      if (body.description !== undefined) group.description = body.description;
      if (body.owner !== undefined) group.owner = body.owner;
      group.updated_by = "admin";
      group.updated_at = new Date().toISOString();
      recomputeMembers(group);
      return { body: group };
    },
  },
  {
    method: "DELETE",
    path: (path) => {
      const m = /^\/groups\/([^/]+)$/.exec(path);
      return m ? { group_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const index = groupsState.findIndex((g) => g.group_id === params.group_id);
      if (index === -1) return { body: { detail: "group not found" }, status: 404 };
      const [removed] = groupsState.splice(index, 1);
      return { body: { deleted: removed.group_id, name: removed.name } };
    },
  },
  {
    method: "POST",
    path: (path) => {
      const m = /^\/groups\/([^/]+)\/preview$/.exec(path);
      return m ? { group_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const group = groupsState.find((g) => g.group_id === params.group_id);
      if (!group) return { body: { detail: "group not found" }, status: 404 };
      const previous = new Set(group.members);
      const now = resolveMockMembers(group);
      const preview = {
        group_id: group.group_id,
        name: group.name,
        count: now.members.length,
        previous_count: group.members.length,
        added: now.members.filter((id) => !previous.has(id)),
        dropped: group.members.filter((id) => !now.members.includes(id)),
        unchanged: now.members.filter((id) => previous.has(id)),
        sources: now.sources,
        unknown_pins: now.unknown_pins,
      };
      group.members = now.members;
      group.sources = now.sources;
      group.unknown_pins = now.unknown_pins;
      group.member_count = now.members.length;
      return { body: preview };
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
    // One-shot validate → push (no proposal step). Same approval semantics as
    // `POST /config/rollouts`: the canary is released, the rest stays pending.
    method: "POST",
    path: "/config/push",
    respond: async (request) => {
      const body = (await request.json()) as {
        scope?: { agents?: string[]; group?: string };
        note?: string | null;
        canary_percent?: number | null;
        max_agents_per_batch?: number | null;
        max_percent_per_batch?: number | null;
        health_gate_seconds?: number | null;
        restart_after_apply?: boolean;
      };
      const denied = sessionActive() ? roleDenied("approver") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;

      const target = body.scope?.agents ?? (body.scope?.group ? groupMembers(body.scope.group) : []);
      if (target.length === 0) {
        return { body: { detail: "the scope resolved to no agents" }, status: 422 };
      }
      const ordered = [...target].sort();
      const canaryPct = body.canary_percent ?? 5;
      const maxAgents = body.max_agents_per_batch ?? 25;
      const maxPct = body.max_percent_per_batch ?? 25;
      const step = Math.max(1, Math.min(maxAgents, Math.ceil((ordered.length * maxPct) / 100)));
      const canary = Math.max(1, Math.min(step, Math.ceil((ordered.length * canaryPct) / 100)));
      const batches = [ordered.slice(0, canary)];
      for (let index = canary; index < ordered.length; index += step) {
        batches.push(ordered.slice(index, index + step));
      }
      const gate = body.health_gate_seconds ?? 90;
      const plan = {
        agents: ordered.length,
        batch: {
          count: batches.length,
          canary_percent: canaryPct,
          max_agents_per_batch: body.max_agents_per_batch ?? null,
          max_percent_per_batch: body.max_percent_per_batch ?? null,
          size: step,
          health_gate_seconds: gate,
          ttl_minutes: 60,
          restart_after_apply: body.restart_after_apply ?? false,
        },
        batches: batches.map((members, index) => ({
          batch: index,
          agents: members.length,
          agent_ids: members,
        })),
      };
      const verdict = { valid: true, structure_checked: true, profile: "otelcol" };

      const proposal: MockProposal = {
        proposal_id: `prop-mock-${nextId.proposal++}`,
        status: "active",
        config_hash: "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        agents: ordered.length,
        revisions: ordered.map((agent_id) => ({
          agent_id,
          revision_id: `rev-${agent_id.slice(0, 4)}-${nextId.revision++}`,
        })),
        note: body.note ?? null,
        created_at: new Date().toISOString(),
      };
      proposals.set(proposal.proposal_id, proposal);
      fixtureRevisions.push(
        ...proposal.revisions.map((r) => ({
          revision_id: r.revision_id,
          agent_id: r.agent_id,
          proposal_id: proposal.proposal_id,
          status: "active",
          config_hash: proposal.config_hash,
          author: "admin",
          created_at: proposal.created_at,
          approved_by: "admin",
          approved_at: proposal.created_at,
          note: proposal.note,
          parent_revision_id: null,
          withdrawn: false,
          failed_at: null,
          failure_detail: null,
          verdict,
          group_id: null,
          group_name: null,
        })),
      );
      const rollout: MockRollout = {
        rollout_id: `roll-mock-${nextId.rollout++}`,
        proposal_id: proposal.proposal_id,
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
          size: step,
          canary_percent: canaryPct,
          max_agents_per_batch: body.max_agents_per_batch ?? null,
          max_percent_per_batch: body.max_percent_per_batch ?? null,
          health_gate_seconds: gate,
          ttl_minutes: 60,
          restart_after_apply: body.restart_after_apply ?? false,
          started_at: new Date().toISOString(),
        },
        agents: {
          total: ordered.length,
          states: {
            delivered: batches[0].length,
            pending: ordered.length - batches[0].length,
          },
        },
        batches: [
          { ...plan.batches[0], status: "in_flight", states: { delivered: batches[0].length } },
          ...plan.batches
            .slice(1)
            .map((b) => ({ ...b, status: "pending", states: { pending: b.agents } })),
        ],
      };
      rolloutsState.push(rollout);
      return {
        body: {
          ...rollout,
          proposal_id: proposal.proposal_id,
          config_hash: proposal.config_hash,
          validate: verdict,
        },
      };
    },
  },
  {
    method: "POST",
    path: "/config/revisions",
    respond: async (request) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
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
      const denied = sessionActive()
        ? roleDenied(params.action === "reject" ? "approver" : "operator")
        : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
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
      const denied = sessionActive() ? roleDenied("approver") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
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
      // abort is operator; resume and rollback are approver (as on the server).
      const denied = sessionActive()
        ? roleDenied(params.action === "abort" ? "operator" : "approver")
        : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
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

  /* ---------------------------------------------------------------- */
  /* Admin pages (stage 8): alerts, tokens, sessions, compliance,      */
  /* updates. Every handler enforces the same role floor the live      */
  /* endpoint does (`docs/api-reference.md`), so the stage-9 role-denial  */
  /* cases exercise the real 403 path against the mock: the server is    */
  /* the authority even when a client-side hint misfires.                */
  /* ---------------------------------------------------------------- */
  {
    method: "GET",
    path: "/alerts",
    respond: () => (sessionActive() ? { body: fixtures.alerts } : { body: UNAUTHORIZED, status: 401 }),
  },
  {
    method: "PUT",
    path: "/alerts",
    respond: async (request) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const body = (await request.json()) as {
        events?: Record<string, Record<string, unknown>>;
      };
      // Round-trip semantics: a sentinel field keeps the stored value.
      const events = fixtures.alerts.config.events as Record<string, Record<string, unknown>>;
      for (const [event, cfg] of Object.entries(body.events ?? {})) {
        const stored = events[event];
        if (stored) {
          for (const key of Object.keys(cfg)) {
            if (cfg[key] === "***REDACTED***" && key in stored) {
              cfg[key] = stored[key];
            }
          }
        }
        events[event] = { ...events[event], ...cfg };
      }
      return { body: { config: fixtures.alerts.config } };
    },
  },
  {
    method: "POST",
    path: "/alerts/test",
    respond: (request) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      void request; // body accepted, outcome fixed — the mock has no webhooks.
      return { body: { success: true, error: null } };
    },
  },
  {
    method: "GET",
    path: "/agent-tokens",
    respond: () =>
      sessionActive() ? { body: fixtures.agentTokens } : { body: UNAUTHORIZED, status: 401 },
  },
  {
    method: "POST",
    path: "/agent-tokens",
    respond: async (request) => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const body = (await request.json().catch(() => ({}))) as {
        label?: string;
        expires_in_days?: number | null;
        max_agents?: number | null;
        agent_type?: string | null;
      };
      if (body.agent_type && !fixtures.agentTypes.types.some((t) => t.name === body.agent_type)) {
        return { body: { detail: `no profile named '${body.agent_type}'` }, status: 422 };
      }
      const token = {
        token_id: `tok_${nextId.token++}`, // pretend-random handle
        label: body.label ?? null,
        agent_id: null,
        agent_type: body.agent_type ?? null,
        created_at: new Date().toISOString(),
        expires_at: body.expires_in_days
          ? new Date(Date.now() + body.expires_in_days * 86_400_000).toISOString()
          : null,
        max_agents: body.max_agents ?? null,
        agents_used: 0,
        last_seen_at: null,
        revoked_at: null,
        expired: false,
        active: true,
      };
      (fixtures.agentTokens.tokens as unknown[]).unshift(token);
      fixtures.agentTokens.count += 1;
      return {
        status: 201,
        body: {
          token: `mock-${token.token_id}-not-a-real-credential`,
          token_id: token.token_id,
          label: token.label,
          agent_type: token.agent_type,
          created_at: token.created_at,
          expires_at: token.expires_at,
          max_agents: token.max_agents,
          endpoint: "http://127.0.0.1:4320/v1/opamp",
          collector_snippet: `# paste into extensions.opampextension\nserver: ${token.token_id}`,
          note: "Shown once and not recoverable.",
        },
      };
    },
  },
  {
    method: "DELETE",
    path: (path) => {
      const m = /^\/agent-tokens\/([^/]+)$/.exec(path);
      return m ? { token_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const token = (fixtures.agentTokens.tokens as { token_id: string; revoked_at: string | null; active: boolean }[])
        .find((t) => t.token_id === params.token_id);
      if (!token || token.revoked_at) {
        return { body: { detail: "no such token, or already revoked" }, status: 404 };
      }
      token.revoked_at = new Date().toISOString();
      token.active = false;
      return { body: { token_id: token.token_id, revoked: true } };
    },
  },
  {
    method: "GET",
    path: "/auth/sessions",
    respond: () => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      return { body: fixtures.adminSessions };
    },
  },
  {
    method: "DELETE",
    path: (path) => {
      const m = /^\/auth\/sessions\/([^/]+)$/.exec(path);
      return m ? { session_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      if (params.session_id.length < 6) {
        return { body: { detail: "session id must be at least 6 characters" }, status: 422 };
      }
      const sessions = fixtures.adminSessions.sessions;
      const idx = sessions.findIndex((s) => s.session_id.startsWith(params.session_id));
      if (idx < 0) return { body: { detail: "no session with that id" }, status: 404 };
      sessions.splice(idx, 1);
      return { body: { session_id: params.session_id, revoked: true } };
    },
  },
  {
    method: "GET",
    path: "/compliance/validate",
    respond: () => ({ body: fixtures.policyValidation }),
  },
  {
    method: "POST",
    path: "/compliance/reload",
    respond: () => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      return { body: { success: true } };
    },
  },
  {
    method: "POST",
    path: (path) => {
      const m = /^\/compliance\/check\/([^/]+)$/.exec(path);
      return m ? { agent_id: m[1] } : null;
    },
    respond: (_request, params) => {
      const denied = sessionActive() ? roleDenied("operator") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const detail = fixtures.agentDetails[params.agent_id];
      if (!detail) return { body: { detail: "Agent not found" }, status: 404 };
      const compliance = (detail as { compliance?: { compliant?: boolean | null } }).compliance;
      return {
        body: {
          agent_id: params.agent_id,
          compliant: compliance?.compliant ?? null,
          violations: [],
        },
      };
    },
  },
  {
    method: "GET",
    path: "/updates",
    respond: () => ({ body: fixtures.updates }),
  },
  {
    method: "PUT",
    path: "/updates/settings",
    respond: async (request) => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      const body = (await request.json()) as { enabled?: boolean | null };
      fixtures.updates.admin_override = body.enabled ?? null;
      fixtures.updates.enabled = body.enabled ?? fixtures.updates.deployment_enabled;
      return { body: fixtures.updates };
    },
  },
  {
    method: "POST",
    path: "/updates/check",
    respond: () => {
      const denied = sessionActive() ? roleDenied("admin") : { body: UNAUTHORIZED, status: 401 };
      if (denied) return denied;
      fixtures.updates.checked_at = new Date().toISOString();
      return { body: fixtures.updates };
    },
  },
];

/** Mutable config-flow state (module scope, like the session flag). */
const nextId = { proposal: 1, revision: 1, rollout: 1, group: 3, token: 3 };
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
  const group = groupsState.find((g) => g.name === name);
  return group ? group.members : [];
}

/** Stage-7 group state: the fixtures' groups, mutable. */
const groupsState: GroupView[] = structuredClone(fixtures.groups.groups);

/**
 * Member resolution stand-in (mirror of `groups.resolve_members`, not a
 * re-implementation): SQL via runSelection, legacy definitions by the
 * `environment` key against `deployment.environment`, plus pins that match
 * a live agent — pins that match nothing stay `unknown_pins`, as on the server.
 */
function resolveMockMembers(group: GroupView): {
  members: string[];
  sources: Record<string, string[]>;
  unknown_pins: string[];
} {
  const agents = fixtures.agents.agents as AgentEntry[];
  const selected = group.query
    ? runSelection(group.query, agents)
    : agents.filter((a) => {
        const env = (group.definition as Record<string, unknown> | null)?.environment;
        return typeof env !== "string" || attrValue(a.description, "deployment.environment") === env;
      });
  const sources: Record<string, string[]> = {
    [group.query ? "query" : "definition"]: selected.map((a) => a.id),
    included: [],
    excluded: [],
  };
  const memberIds = new Set(selected.map((a) => a.id));
  const unknown_pins: string[] = [];
  for (const id of group.include) {
    if (memberIds.has(id)) continue;
    if (agents.some((a) => a.id === id)) {
      sources.included.push(id);
      memberIds.add(id);
    } else {
      unknown_pins.push(id);
    }
  }
  for (const id of group.exclude) {
    if (memberIds.delete(id)) sources.excluded.push(id);
  }
  return { members: [...memberIds], sources, unknown_pins };
}

function recomputeMembers(group: GroupView): void {
  const now = resolveMockMembers(group);
  if (now.members.join(",") !== group.members.join(",")) group.membership_changes++;
  group.members = now.members;
  group.sources = now.sources;
  group.unknown_pins = now.unknown_pins;
  group.member_count = now.members.length;
  group.health_defined =
    group.query && /healthy/i.test(group.query)
      ? ["healthy"]
      : group.definition && "healthy" in (group.definition as object)
        ? ["healthy"]
        : [];
}

function mockGroup(
  name: string,
  query: string | null,
  definition: Record<string, unknown> | null,
  include: string[],
  exclude: string[],
  description: string | null,
  owner: string | null,
): GroupView {
  const group: GroupView = {
    group_id: `grp-mock-${nextId.group++}`,
    name,
    selector_kind: query ? "query" : "definition",
    query,
    definition,
    include,
    exclude,
    description,
    owner,
    source: "api",
    created_by: "admin",
    created_at: new Date().toISOString(),
    updated_by: null,
    updated_at: null,
    member_count: 0,
    members: [],
    sources: null,
    unknown_pins: [],
    health_defined: [],
    membership_changes: 0,
  };
  recomputeMembers(group);
  return group;
}

/**
 * Auth-stateful handlers. The browser's real credential is the httpOnly cookie
 * the server sets; here a module-level flag stands in for it so the login →
 * shell → logout round-trip is exercisable without a live server (and by
 * Playwright, which never sees a real cookie jar against mock mode).
 */
let sessionLive = false;

/**
 * Empty-fleet variant for the zero-agent screens (Overview quickstart,
 * `e2e/quickstart.spec.ts`): `GET /agents` answers `{agents: [], count: 0}`
 * while this key is set in localStorage. A page can flip it mid-test to watch
 * the console move from "connect your first agent" to a connected fleet
 * without a second dev server or a second build.
 *
 * ```js
 * localStorage.setItem("opamp-mock-empty-fleet", "1");
 * ```
 */
const MOCK_EMPTY_FLEET_KEY = "opamp-mock-empty-fleet";

function emptyFleet(): boolean {
  if (typeof localStorage === "undefined") return false;
  return localStorage.getItem(MOCK_EMPTY_FLEET_KEY) === "1";
}

/** Role of the standing mock session (stage 9): name → role on login. */
let sessionRole = "admin";

/**
 * The mock session must survive full page navigations (Playwright `goto`
 * reloads the module): in a browser the flag is mirrored into localStorage —
 * same spirit as the httpOnly cookie it stands in for. In node (vitest) the
 * module flag alone is used.
 */
const MOCK_SESSION_KEY = "opamp-mock-session-live";
const MOCK_SESSION_ROLE_KEY = "opamp-mock-session-role";

function sessionActive(): boolean {
  if (typeof localStorage === "undefined") return sessionLive;
  return sessionLive || localStorage.getItem(MOCK_SESSION_KEY) === "1";
}

/** The signed-in principal's role (name → role at login, admin by default). */
function currentRole(): string {
  if (typeof localStorage === "undefined") return sessionRole;
  return localStorage.getItem(MOCK_SESSION_ROLE_KEY) ?? sessionRole;
}

function setSessionLive(on: boolean, role = sessionRole): void {
  sessionLive = on;
  sessionRole = role;
  try {
    if (on) {
      localStorage.setItem(MOCK_SESSION_KEY, "1");
      localStorage.setItem(MOCK_SESSION_ROLE_KEY, role);
    } else {
      localStorage.removeItem(MOCK_SESSION_KEY);
      localStorage.removeItem(MOCK_SESSION_ROLE_KEY);
    }
  } catch {
    /* storage unavailable (node, privacy mode) — the flag still works */
  }
}

/**
 * Role gate mirroring `server/auth.py::require_roles`: 403 with the server's
 * exact detail wording when the standing session's role is below `required`.
 * Roles are ordered (viewer < operator < approver < admin) and imply every
 * lesser one.
 */
const ROLE_RANK: Record<string, number> = {
  viewer: 1,
  operator: 2,
  approver: 3,
  admin: 4,
};

function roleDenied(required: string): { body: unknown; status: number } | null {
  const role = currentRole();
  if ((ROLE_RANK[role] ?? 0) >= (ROLE_RANK[required] ?? 0)) return null;
  return {
    body: {
      detail: `role '${role}' cannot perform this action (requires '${required}')`,
    },
    status: 403,
  };
}

/** `GET /auth/me` for the standing session, role included. */
function meBody() {
  const role = currentRole();
  return {
    ...fixtures.me,
    name: role === "admin" ? "admin" : role,
    role,
  };
}

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
