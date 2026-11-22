import { describe, expect, it } from "vitest";
import { mockFetch } from "./mock";

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
