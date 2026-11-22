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
