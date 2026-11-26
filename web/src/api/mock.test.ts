import { describe, expect, it, beforeAll } from "vitest";
import { mockFetch } from "./mock";
import { fixtures } from "./fixtures";

const BASE = "http://localhost:5173/api";

describe("mockFetch", () => {
  it("serves /agents from fixtures", async () => {
    const res = await mockFetch("http://localhost:5173/api/agents");
    expect(res.status).toBe(200);
    const body = (await res.json()) as { count: number; agents: { agent_type: string }[] };
    expect(body.count).toBe(body.agents.length);
    // Fixtures must exercise more than one agent type — stage 5's type-aware
    // views are tested against these.
    expect(new Set(body.agents.map((a) => a.agent_type)).size).toBeGreaterThan(1);
  });

  it("unknown routes answer 404 instead of falling through to the network", async () => {
    const res = await mockFetch("http://localhost:5173/api/nope");
    expect(res.status).toBe(404);
  });

  it("tolerates a trailing slash on the base path", async () => {
    const res = await mockFetch("http://localhost:5173/api/auth/status/");
    expect(res.status).toBe(200);
  });

  it("serves /agent/types with more than one profile", async () => {
    const res = await mockFetch("http://localhost:5173/api/agent/types");
    expect(res.status).toBe(200);
    const body = (await res.json()) as { types: { name: string }[] };
    expect(body.types.map((t) => t.name)).toContain("otelcol");
    expect(body.types.map((t) => t.name)).toContain("generic");
  });

  it("serves /agent/{id} for fixture agents and 404s unknown ids", async () => {
    const res = await mockFetch("http://localhost:5173/api/agent/6b1f3a4c-9d2e-4f70-8a1b-2c3d4e5f6071");
    expect(res.status).toBe(200);
    const body = (await res.json()) as { agent_type: string; metrics: unknown };
    expect(body.agent_type).toBe("otelcol");
    expect(body.metrics).toBeTruthy();

    const missing = await mockFetch("http://localhost:5173/api/agent/00000000-0000-4000-8000-000000000000");
    expect(missing.status).toBe(404);
  });

  it("`q` selects server-side: unhealthy agents only", async () => {
    const res = await mockFetch(
      "http://localhost:5173/api/agents?q=" +
        encodeURIComponent("SELECT * FROM agents WHERE healthy = 0"),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { count: number; agents: { healthy: boolean }[] };
    expect(body.count).toBe(1);
    expect(body.agents[0].healthy).toBe(false);
  });

  it("`q` selects server-side: by agent type", async () => {
    const res = await mockFetch(
      "http://localhost:5173/api/agents?q=" +
        encodeURIComponent("SELECT * FROM agents WHERE type = 'otelcol'"),
    );
    const body = (await res.json()) as { count: number; agents: { agent_type: string }[] };
    expect(body.count).toBe(1);
    expect(body.agents[0].agent_type).toBe("otelcol");
  });
});

describe("mockFetch — admin pages (stage 8)", () => {
  const login = () =>
    mockFetch(`${BASE}/auth/session`, {
      method: "POST",
      body: JSON.stringify({ name: "admin", password: fixtures.validPassword }),
    });

  it("admin reads are 401 before any session exists", async () => {
    const res = await mockFetch(`${BASE}/alerts`);
    expect(res.status).toBe(401);
  });

  describe("signed in", () => {
    beforeAll(async () => {
      const res = await login();
      expect(res.status).toBe(201);
    });

    it("alerts round-trip: a sentinel field keeps the stored value", async () => {
      const put = await mockFetch(`${BASE}/alerts`, {
        method: "PUT",
        body: JSON.stringify({
          events: {
            new_agent: {
              enabled: true,
              webhook_url: "***REDACTED***",
              headers: "***REDACTED***",
              body_template: "{}",
            },
          },
        }),
      });
      expect(put.status).toBe(200);
      const body = (await put.json()) as {
        config: { events: Record<string, { webhook_url: string; body_template: string }> };
      };
      // The sentinel comes back redacted (the stored secret was kept), while
      // the non-secret body template round-trips as sent.
      expect(body.config.events.new_agent.webhook_url).toContain("***REDACTED***");
      expect(body.config.events.new_agent.body_template).toBe("{}");
    });

    it("minting a fleet token shows it once, then only the handle", async () => {
      const post = await mockFetch(`${BASE}/agent-tokens`, {
        method: "POST",
        body: JSON.stringify({ label: "test fleet", max_agents: 3, agent_type: "otelcol" }),
      });
      expect(post.status).toBe(201);
      const minted = (await post.json()) as { token: string; token_id: string; collector_snippet: string };
      expect(minted.token).toContain("not-a-real-credential");
      expect(minted.collector_snippet).toBeTruthy();

      const list = await mockFetch(`${BASE}/agent-tokens`);
      const listed = (await list.json()) as { tokens: { token_id: string; token?: string }[] };
      expect(listed.tokens.map((t) => t.token_id)).toContain(minted.token_id);
      expect(listed.tokens.some((t) => t.token !== undefined)).toBe(false);

      const revoke = await mockFetch(`${BASE}/agent-tokens/${minted.token_id}`, { method: "DELETE" });
      expect(revoke.status).toBe(200);
      const again = await mockFetch(`${BASE}/agent-tokens/${minted.token_id}`, { method: "DELETE" });
      expect(again.status).toBe(404);
    });

    it("minting rejects an unknown agent type with 422", async () => {
      const post = await mockFetch(`${BASE}/agent-tokens`, {
        method: "POST",
        body: JSON.stringify({ agent_type: "not-a-profile" }),
      });
      expect(post.status).toBe(422);
    });

    it("sessions list + revoke by short id, 422 for a too-short id", async () => {
      const list = await mockFetch(`${BASE}/auth/sessions`);
      const body = (await list.json()) as { sessions: { session_id: string; expired: boolean }[] };
      expect(body.sessions.length).toBeGreaterThan(0);
      const live = body.sessions.find((s) => !s.expired);
      expect(live).toBeTruthy();

      const short = await mockFetch(`${BASE}/auth/sessions/abc`, { method: "DELETE" });
      expect(short.status).toBe(422);

      const revoke = await mockFetch(`${BASE}/auth/sessions/${live!.session_id}`, { method: "DELETE" });
      expect(revoke.status).toBe(200);
      const gone = await mockFetch(`${BASE}/auth/sessions/${live!.session_id}`, { method: "DELETE" });
      expect(gone.status).toBe(404);
    });

    it("compliance: validation lists valid and invalid policies; check works per agent", async () => {
      const validate = await mockFetch(`${BASE}/compliance/validate`);
      const body = (await validate.json()) as {
        opa_enabled: boolean;
        policies: { valid: boolean }[];
      };
      expect(body.opa_enabled).toBe(true);
      expect(body.policies.some((p) => p.valid)).toBe(true);
      expect(body.policies.some((p) => !p.valid)).toBe(true);

      const agentId = "6b1f3a4c-9d2e-4f70-8a1b-2c3d4e5f6071";
      const check = await mockFetch(`${BASE}/compliance/check/${agentId}`, { method: "POST" });
      expect(check.status).toBe(200);
      const verdict = (await check.json()) as { compliant: boolean | null };
      expect("compliant" in verdict).toBe(true);

      const missing = await mockFetch(`${BASE}/compliance/check/00000000-0000-4000-8000-000000000000`, { method: "POST" });
      expect(missing.status).toBe(404);
    });

    it("updates: settings toggle clears, check refreshes checked_at", async () => {
      const before = await (await mockFetch(`${BASE}/updates`)).json();
      const state = before as { checked_at: string; enabled: boolean; deployment_enabled: boolean };

      const put = await mockFetch(`${BASE}/updates/settings`, {
        method: "PUT",
        body: JSON.stringify({ enabled: false }),
      });
      expect(put.status).toBe(200);
      const after = (await put.json()) as { enabled: boolean; admin_override: boolean | null };
      expect(after.enabled).toBe(false);
      expect(after.admin_override).toBe(false);

      // Clearing the override returns to the deployment default.
      const cleared = (await (
        await mockFetch(`${BASE}/updates/settings`, {
          method: "PUT",
          body: JSON.stringify({ enabled: null }),
        })
      ).json()) as { enabled: boolean; admin_override: boolean | null };
      expect(cleared.enabled).toBe(state.deployment_enabled);
      expect(cleared.admin_override).toBeNull();

      const check = (await (await mockFetch(`${BASE}/updates/check`, { method: "POST" })).json()) as {
        checked_at: string;
      };
      expect(check.checked_at).toBeTruthy();
      expect(check.checked_at).not.toBe(state.checked_at);
    });
  });
});

/**
 * Role-denial cases (stage 9): the mock enforces the same role floor the live
 * endpoints do, so the 403 contract — session kept, `role '<x>' cannot
 * perform this action (requires '<y>')` — is testable without a server.
 * Runs last: the logins here change the standing session's role.
 */
describe("mockFetch — role denial (stage 9)", () => {
  const loginAs = async (name: string) => {
    const res = await mockFetch(`${BASE}/auth/session`, {
      method: "POST",
      body: JSON.stringify({ name, password: fixtures.validPassword }),
    });
    expect(res.status).toBe(201);
    const body = (await res.json()) as { principal: { role: string } };
    return body.principal.role;
  };

  it("a login name maps onto its principal's role", async () => {
    expect(await loginAs("viewer")).toBe("viewer");
    const me = await mockFetch(`${BASE}/auth/me`);
    expect(((await me.json()) as { role: string }).role).toBe("viewer");
  });

  it("viewer keeps the session on a 403: reads answer, mutations refuse", async () => {
    const read = await mockFetch(`${BASE}/alerts`);
    expect(read.status).toBe(200); // GET /alerts is viewer-level on the server

    const put = await mockFetch(`${BASE}/alerts`, {
      method: "PUT",
      body: JSON.stringify({ events: {} }),
    });
    expect(put.status).toBe(403);
    const detail = (await put.json()) as { detail: string };
    // Exact server wording — web/src/api/errors.ts keys off it.
    expect(detail.detail).toBe(
      "role 'viewer' cannot perform this action (requires 'operator')",
    );

    // The session survived the refusal (403 never drops it — only 401 does).
    const still = await mockFetch(`${BASE}/auth/me`);
    expect(still.status).toBe(200);

    const testAlert = await mockFetch(`${BASE}/alerts/test`, { method: "POST" });
    expect(testAlert.status).toBe(403);
  });

  it("operator passes operator gates but not admin ones", async () => {
    expect(await loginAs("operator")).toBe("operator");

    const save = await mockFetch(`${BASE}/alerts`, {
      method: "PUT",
      body: JSON.stringify({ events: {} }),
    });
    expect(save.status).toBe(200);

    const mint = await mockFetch(`${BASE}/agent-tokens`, {
      method: "POST",
      body: JSON.stringify({ label: "denied" }),
    });
    expect(mint.status).toBe(403);
    const detail = (await mint.json()) as { detail: string };
    expect(detail.detail).toContain("requires 'admin'");

    const sessions = await mockFetch(`${BASE}/auth/sessions`);
    expect(sessions.status).toBe(403);

    const reload = await mockFetch(`${BASE}/compliance/reload`, { method: "POST" });
    expect(reload.status).toBe(403);

    const checkNow = await mockFetch(`${BASE}/updates/check`, { method: "POST" });
    expect(checkNow.status).toBe(403);
  });

  it("approver can roll out but cannot mint tokens or revoke sessions", async () => {
    expect(await loginAs("approver")).toBe("approver");

    const propose = await mockFetch(`${BASE}/config/revisions`, {
      method: "POST",
      body: JSON.stringify({ scope: { agents: ["6b1f3a4c-9d2e-4f70-8a1b-2c3d4e5f6071"] } }),
    });
    expect(propose.status).toBe(200); // propose is operator; approver outranks it
    const proposal = (await propose.json()) as { proposal_id: string };

    const rollout = await mockFetch(
      `${BASE}/config/rollouts?proposal_id=${proposal.proposal_id}`,
      { method: "POST", body: JSON.stringify({ dry_run: true }) },
    );
    expect(rollout.status).toBe(200); // rollouts need approver — exactly this role

    const sessions = await mockFetch(`${BASE}/auth/sessions`);
    expect(sessions.status).toBe(403);
  });

  it("the ladder is ordered: admin satisfies everything below it", async () => {
    expect(await loginAs("admin")).toBe("admin");
    const sessions = await mockFetch(`${BASE}/auth/sessions`);
    expect(sessions.status).toBe(200); // admin-only endpoint, admin role
  });
});
