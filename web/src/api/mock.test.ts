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
});
