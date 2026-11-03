import { afterEach, describe, expect, it, vi } from "vitest";

import worker from "../src/index";
import type { Env } from "../src/env";
import {
  isValidEmail,
  isValidSemver,
  randomToken,
  sha256Hex,
} from "../src/validate";

class FakeKV {
  readonly store = new Map<string, string>();

  async get(key: string, type?: string): Promise<unknown> {
    const value = this.store.get(key);
    if (value === undefined) return null;
    return type === "json" ? JSON.parse(value) : value;
  }

  async put(
    key: string,
    value: string,
    _options?: { expirationTtl?: number },
  ): Promise<void> {
    this.store.set(key, value);
  }

  async delete(key: string): Promise<void> {
    this.store.delete(key);
  }
}

interface MockCall {
  url: string;
  method: string;
  body: any;
  headers: Headers;
}

function makeEnv(overrides: Partial<Env> = {}): Env {
  return {
    UPDATES_KV: new FakeKV() as unknown as KVNamespace,
    RESEND_API_KEY: "re_test",
    RESEND_SEGMENT_ID: "seg_test",
    RESEND_FROM: "opamp-server-py <updates@example.com>",
    GITHUB_WEBHOOK_SECRET: "whsec",
    PUBLIC_BASE_URL: "https://updates.test",
    REPO_URL: "https://github.com/agardnerIT/opamp-server-py",
    ...overrides,
  };
}

function installFetch(
  handler: (call: MockCall) => Response | Promise<Response>,
): MockCall[] {
  const calls: MockCall[] = [];
  const mocked = vi.fn(async (input: any, init: any = {}) => {
    const url =
      typeof input === "string" ? input : input?.url ?? String(input);
    let body = init.body;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch {
        // Keep the raw string (e.g. a form body or a webhook payload).
      }
    }
    const call: MockCall = {
      url,
      method: String(init.method ?? "GET").toUpperCase(),
      body,
      headers: new Headers(init.headers ?? {}),
    };
    calls.push(call);
    return handler(call);
  });
  vi.stubGlobal("fetch", mocked);
  return calls;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function subscribeRequest(
  fields: Record<string, string>,
  acceptJson = true,
): Request {
  const headers: Record<string, string> = {
    "content-type": "application/x-www-form-urlencoded",
  };
  if (acceptJson) headers.accept = "application/json";
  return new Request("https://updates.test/subscribe", {
    method: "POST",
    headers,
    body: new URLSearchParams(fields).toString(),
  });
}

function deleteRequest(
  fields: Record<string, string>,
  acceptJson = true,
): Request {
  const headers: Record<string, string> = {
    "content-type": "application/x-www-form-urlencoded",
  };
  if (acceptJson) headers.accept = "application/json";
  return new Request("https://updates.test/delete", {
    method: "POST",
    headers,
    body: new URLSearchParams(fields).toString(),
  });
}

async function githubSignature(secret: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(body),
  );
  const hex = Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `sha256=${hex}`;
}

function releaseRequest(body: string, signature: string): Request {
  return new Request("https://updates.test/release", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-github-event": "release",
      "x-hub-signature-256": signature,
    },
    body,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("validation", () => {
  it("accepts ordinary addresses and rejects junk", () => {
    expect(isValidEmail("a@b.co")).toBe(true);
    expect(isValidEmail("user+tag@example.com")).toBe(true);
    expect(isValidEmail("a@b")).toBe(false);
    expect(isValidEmail("")).toBe(false);
    expect(isValidEmail("a b@example.com")).toBe(false);
  });

  it("accepts semver tags and rejects everything else", () => {
    expect(isValidSemver("v0.2.0")).toBe(true);
    expect(isValidSemver("0.2.0")).toBe(true);
    expect(isValidSemver("0.2.0-rc.1")).toBe(true);
    expect(isValidSemver("latest")).toBe(false);
    expect(isValidSemver("v0.2")).toBe(false);
    expect(isValidSemver("<script>")).toBe(false);
  });

  it("makes 43-character base64url tokens", () => {
    const token = randomToken();
    expect(token).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(randomToken()).not.toBe(token);
  });
});

describe("health", () => {
  it("reports configuration without exposing secrets", async () => {
    const response = await worker.fetch(
      new Request("https://updates.test/health"),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    const text = await response.text();
    const body = JSON.parse(text);
    expect(body.ok).toBe(true);
    expect(body.configured.resend).toBe(true);
    expect(body.configured.github_webhook).toBe(true);
    expect(body.configured.turnstile).toBe(false);
    expect(text).not.toContain("re_test");
    expect(text).not.toContain("whsec");
  });
});

describe("signup form", () => {
  it("renders email and consent fields", async () => {
    const response = await worker.fetch(
      new Request("https://updates.test/"),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    const html = await response.text();
    expect(html).toContain('name="email"');
    expect(html).toContain('name="consent"');
    expect(html).toContain('action="/subscribe"');
    expect(html).not.toContain("challenges.cloudflare.com");
  });

  it("includes Turnstile when a site key is configured", async () => {
    const response = await worker.fetch(
      new Request("https://updates.test/"),
      makeEnv({ TURNSTILE_SITE_KEY: "site_key" }),
    );
    const html = await response.text();
    expect(html).toContain("challenges.cloudflare.com/turnstile");
    expect(html).toContain('data-sitekey="site_key"');
  });
});

describe("subscribe", () => {
  it("sends one confirmation email and stores a pending token", async () => {
    const calls = installFetch(() => json({ id: "email_1" }));
    const env = makeEnv();

    const response = await worker.fetch(
      subscribeRequest({ email: "User@Example.com", consent: "on" }),
      env,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ ok: true });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://api.resend.com/emails");
    expect(calls[0].body.to).toEqual(["user@example.com"]);
    expect(calls[0].body.idempotencyKey).toBeUndefined(); // header, not body
    expect(calls[0].headers.get("idempotency-key")).toMatch(/^confirm-[0-9a-f]{64}-[A-Za-z0-9_-]{43}$/);
    expect(calls[0].body.text).toContain("https://updates.test/confirm?token=");
    expect(calls[0].body.text).toContain("24 hours");

    const token = /token=([A-Za-z0-9_-]{43})/.exec(calls[0].body.text)?.[1];
    expect(token).toBeTruthy();
    expect(await env.UPDATES_KV.get(`pending:${token}`)).toBeTruthy();
    expect(
      await env.UPDATES_KV.get(`sent:${await sha256Hex("user@example.com")}`),
    ).toBe("1");
  });

  it("returns a 303 for a form submit instead of resubmitting", async () => {
    installFetch(() => json({ id: "email_1" }));
    const response = await worker.fetch(
      subscribeRequest({ email: "user@example.com", consent: "on" }, false),
      makeEnv(),
    );
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/?state=sent");
  });

  it("does not send a second email inside the dedupe window", async () => {
    const calls = installFetch(() => json({ id: "email_1" }));
    const env = makeEnv();

    await worker.fetch(
      subscribeRequest({ email: "user@example.com", consent: "on" }),
      env,
    );
    await worker.fetch(
      subscribeRequest({ email: "user@example.com", consent: "on" }),
      env,
    );

    expect(calls).toHaveLength(1);
  });

  it("rejects a missing consent tick without sending", async () => {
    const calls = installFetch(() => json({ id: "email_1" }));
    const response = await worker.fetch(
      subscribeRequest({ email: "user@example.com" }),
      makeEnv(),
    );
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("returns the error status for a browser form submit", async () => {
    installFetch(() => json({ id: "email_1" }));
    const response = await worker.fetch(
      subscribeRequest({ email: "user@example.com" }, false),
      makeEnv(),
    );
    expect(response.status).toBe(400);
    expect(await response.text()).toContain("tick the box");
  });

  it("rejects an invalid address without sending", async () => {
    const calls = installFetch(() => json({ id: "email_1" }));
    const response = await worker.fetch(
      subscribeRequest({ email: "not-an-email", consent: "on" }),
      makeEnv(),
    );
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("fails closed when Turnstile rejects the token", async () => {
    const calls = installFetch((call) => {
      if (call.url.includes("siteverify")) return json({ success: false });
      return json({ id: "email_1" });
    });
    const response = await worker.fetch(
      subscribeRequest({
        email: "user@example.com",
        consent: "on",
        "cf-turnstile-response": "bad-token",
      }),
      makeEnv({ TURNSTILE_SECRET: "ts", TURNSTILE_SITE_KEY: "site" }),
    );
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain("siteverify");
  });

  it("clears the pending token when Resend refuses the confirmation", async () => {
    installFetch(() => json({ message: "nope" }, 500));
    const env = makeEnv();
    const response = await worker.fetch(
      subscribeRequest({ email: "user@example.com", consent: "on" }),
      env,
    );
    expect(response.status).toBe(502);
    expect((env.UPDATES_KV as unknown as FakeKV).store.size).toBe(0);
  });

  it("rejects an oversized body", async () => {
    installFetch(() => json({ id: "email_1" }));
    const response = await worker.fetch(
      subscribeRequest({
        email: "user@example.com",
        consent: "on",
        padding: "x".repeat(9000),
      }),
      makeEnv(),
    );
    expect(response.status).toBe(413);
  });

  it("never logs the address", async () => {
    installFetch(() => json({ id: "email_1" }));
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    await worker.fetch(
      subscribeRequest({ email: "user@example.com", consent: "on" }),
      makeEnv(),
    );

    const output = [...log.mock.calls, ...error.mock.calls].flat().join(" ");
    expect(output).not.toContain("user@example.com");
  });
});

describe("confirm", () => {
  it("creates a contact in the dedicated segment and consumes the token", async () => {
    const env = makeEnv();
    const token = randomToken();
    await env.UPDATES_KV.put(
      `pending:${token}`,
      JSON.stringify({ email: "user@example.com", createdAt: new Date().toISOString() }),
    );

    const calls = installFetch((call) => {
      if (call.method === "GET") return json({ message: "not found" }, 404);
      if (call.method === "POST" && call.url.endsWith("/contacts")) {
        return json({ id: "contact_1" });
      }
      throw new Error(`unexpected ${call.method} ${call.url}`);
    });

    const response = await worker.fetch(
      new Request(`https://updates.test/confirm?token=${token}`),
      env,
    );

    expect(response.status).toBe(200);
    expect(await response.text()).toContain("Subscription confirmed");
    expect(await env.UPDATES_KV.get(`pending:${token}`)).toBeNull();
    expect(calls).toHaveLength(2);
    expect(calls[1].body.email).toBe("user@example.com");
    expect(calls[1].body.unsubscribed).toBe(false);
    expect(calls[1].body.segments).toEqual([{ id: "seg_test" }]);
  });

  it("revives an existing contact and re-adds it to the segment", async () => {
    const env = makeEnv();
    const token = randomToken();
    await env.UPDATES_KV.put(
      `pending:${token}`,
      JSON.stringify({ email: "user@example.com", createdAt: new Date().toISOString() }),
    );

    const calls = installFetch((call) => {
      if (call.method === "GET") return json({ id: "contact_1" });
      if (call.method === "PATCH") return json({ id: "contact_1" });
      if (call.method === "POST") return json({ id: "contact_1" });
      throw new Error(`unexpected ${call.method} ${call.url}`);
    });

    const response = await worker.fetch(
      new Request(`https://updates.test/confirm?token=${token}`),
      env,
    );

    expect(response.status).toBe(200);
    expect(calls.map((call) => call.method)).toEqual(["GET", "PATCH", "POST"]);
    expect(calls[1].body.unsubscribed).toBe(false);
    expect(calls[2].url).toContain(
      "/contacts/user%40example.com/segments/seg_test",
    );
  });

  it("rejects a replayed or unknown token", async () => {
    const calls = installFetch(() => json({}));
    const response = await worker.fetch(
      new Request(`https://updates.test/confirm?token=${randomToken()}`),
      makeEnv(),
    );
    expect(response.status).toBe(410);
    expect(calls).toHaveLength(0);
  });

  it("keeps the token when Resend fails so the link can be retried", async () => {
    const env = makeEnv();
    const token = randomToken();
    await env.UPDATES_KV.put(
      `pending:${token}`,
      JSON.stringify({ email: "user@example.com", createdAt: new Date().toISOString() }),
    );
    installFetch(() => json({ message: "nope" }, 500));

    const response = await worker.fetch(
      new Request(`https://updates.test/confirm?token=${token}`),
      env,
    );

    expect(response.status).toBe(502);
    expect(await env.UPDATES_KV.get(`pending:${token}`)).toBeTruthy();
  });
});

describe("privacy and deletion", () => {
  it("serves the privacy policy and links to deletion", async () => {
    const response = await worker.fetch(
      new Request("https://updates.test/privacy"),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    const html = await response.text();
    expect(html).toContain("Privacy policy");
    expect(html).toContain("privacy@agardner.net");
    expect(html).toContain("/delete");
    expect(html).toContain("No open pixels");
  });

  it("serves a deletion form", async () => {
    const response = await worker.fetch(
      new Request("https://updates.test/delete"),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    const html = await response.text();
    expect(html).toContain('action="/delete"');
    expect(html).toContain('name="email"');
    expect(html).toContain("Nothing changes until you click");
  });

  it("emails a deletion link only for a member of the segment", async () => {
    const calls = installFetch((call) => {
      if (call.method === "GET" && call.url.includes("/segments")) {
        return json({ data: [{ id: "seg_test" }] });
      }
      if (call.method === "POST" && call.url.endsWith("/emails")) {
        return json({ id: "email_1" });
      }
      throw new Error(`unexpected ${call.method} ${call.url}`);
    });
    const env = makeEnv();

    const response = await worker.fetch(
      deleteRequest({ email: "User@Example.com" }),
      env,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ ok: true });
    expect(calls).toHaveLength(2);
    expect(calls[0].url).toContain(
      "/contacts/user%40example.com/segments",
    );
    expect(calls[1].body.to).toEqual(["user@example.com"]);
    expect(calls[1].body.subject).toContain("deletion");
    expect(calls[1].body.text).toContain(
      "https://updates.test/delete/confirm?token=",
    );
    expect(calls[1].headers.get("idempotency-key")).toMatch(/^delete-/);

    const token = /token=([A-Za-z0-9_-]{43})/.exec(calls[1].body.text)?.[1];
    expect(token).toBeTruthy();
    expect(await env.UPDATES_KV.get(`delete:${token}`)).toBeTruthy();
  });

  it("says nothing and sends nothing for an address not in the segment", async () => {
    const calls = installFetch(() =>
      json({ data: [{ id: "another_segment" }] }),
    );
    const response = await worker.fetch(
      deleteRequest({ email: "user@example.com" }),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ ok: true });
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("GET");
  });

  it("says nothing for a contact that does not exist", async () => {
    const calls = installFetch(() => json({ message: "not found" }, 404));
    const response = await worker.fetch(
      deleteRequest({ email: "user@example.com" }),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ ok: true });
    expect(calls).toHaveLength(1);
  });

  it("does not email twice inside the deletion dedupe window", async () => {
    const calls = installFetch((call) => {
      if (call.method === "GET") return json({ data: [{ id: "seg_test" }] });
      if (call.method === "POST") return json({ id: "email_1" });
      throw new Error("unexpected");
    });
    const env = makeEnv();
    await worker.fetch(deleteRequest({ email: "user@example.com" }), env);
    await worker.fetch(deleteRequest({ email: "user@example.com" }), env);
    expect(calls).toHaveLength(2);
  });

  it("rejects an invalid deletion address without touching Resend", async () => {
    const calls = installFetch(() => json({}));
    const response = await worker.fetch(
      deleteRequest({ email: "not-an-email" }),
      makeEnv(),
    );
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("removes the segment membership and deletes the orphan contact", async () => {
    const env = makeEnv();
    const token = randomToken();
    await env.UPDATES_KV.put(
      `delete:${token}`,
      JSON.stringify({ email: "user@example.com", createdAt: new Date().toISOString() }),
    );

    let reads = 0;
    const calls = installFetch((call) => {
      if (call.method === "GET" && call.url.includes("/segments")) {
        reads += 1;
        return json({ data: reads === 1 ? [{ id: "seg_test" }] : [] });
      }
      if (call.method === "DELETE") return json({ deleted: true });
      throw new Error(`unexpected ${call.method} ${call.url}`);
    });

    const response = await worker.fetch(
      new Request(`https://updates.test/delete/confirm?token=${token}`),
      env,
    );

    expect(response.status).toBe(200);
    expect(await response.text()).toContain("Deleted");
    expect(await env.UPDATES_KV.get(`delete:${token}`)).toBeNull();
    expect(calls.map((call) => call.method)).toEqual([
      "GET",
      "DELETE",
      "GET",
      "DELETE",
    ]);
    expect(calls[1].url).toContain(
      "/contacts/user%40example.com/segments/seg_test",
    );
    expect(calls[3].url).toContain("/contacts/user%40example.com");
  });

  it("keeps a contact that still belongs to another segment", async () => {
    const env = makeEnv();
    const token = randomToken();
    await env.UPDATES_KV.put(
      `delete:${token}`,
      JSON.stringify({ email: "user@example.com", createdAt: new Date().toISOString() }),
    );

    let reads = 0;
    const calls = installFetch((call) => {
      if (call.method === "GET" && call.url.includes("/segments")) {
        reads += 1;
        return json({
          data: reads === 1 ? [{ id: "seg_test" }, { id: "other" }] : [{ id: "other" }],
        });
      }
      if (call.method === "DELETE") return json({ deleted: true });
      throw new Error(`unexpected ${call.method} ${call.url}`);
    });

    const response = await worker.fetch(
      new Request(`https://updates.test/delete/confirm?token=${token}`),
      env,
    );

    expect(response.status).toBe(200);
    expect(calls.map((call) => call.method)).toEqual(["GET", "DELETE", "GET"]);
  });

  it("rejects a replayed deletion token", async () => {
    const calls = installFetch(() => {
      throw new Error("unexpected fetch");
    });
    const response = await worker.fetch(
      new Request(`https://updates.test/delete/confirm?token=${randomToken()}`),
      makeEnv(),
    );
    expect(response.status).toBe(410);
    expect(calls).toHaveLength(0);
  });

  it("keeps the deletion token when Resend fails", async () => {
    const env = makeEnv();
    const token = randomToken();
    await env.UPDATES_KV.put(
      `delete:${token}`,
      JSON.stringify({ email: "user@example.com", createdAt: new Date().toISOString() }),
    );
    installFetch((call) => {
      if (call.method === "GET") return json({ data: [{ id: "seg_test" }] });
      return json({ message: "nope" }, 500);
    });

    const response = await worker.fetch(
      new Request(`https://updates.test/delete/confirm?token=${token}`),
      env,
    );

    expect(response.status).toBe(502);
    expect(await env.UPDATES_KV.get(`delete:${token}`)).toBeTruthy();
  });

  it("never logs the address during deletion", async () => {
    installFetch((call) => {
      if (call.method === "GET") return json({ data: [{ id: "seg_test" }] });
      return json({ id: "email_1" });
    });
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    await worker.fetch(
      deleteRequest({ email: "user@example.com" }),
      makeEnv(),
    );

    const output = [...log.mock.calls, ...error.mock.calls].flat().join(" ");
    expect(output).not.toContain("user@example.com");
  });
});

describe("GitHub release webhook", () => {
  it("sends one broadcast with the unsubscribe merge tag", async () => {
    const secret = "whsec";
    const body = JSON.stringify({
      action: "published",
      release: {
        id: 123,
        tag_name: "v0.2.0",
        draft: false,
        prerelease: false,
      },
    });
    const calls = installFetch(() => json({ id: "broadcast_1" }));

    const response = await worker.fetch(
      releaseRequest(body, await githubSignature(secret, body)),
      makeEnv(),
    );

    expect(response.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://api.resend.com/broadcasts");
    expect(calls[0].body.segment_id).toBe("seg_test");
    expect(calls[0].body.send).toBe(true);
    expect(calls[0].body.subject).toContain("v0.2.0");
    expect(calls[0].body.text).toContain(
      "https://github.com/agardnerIT/opamp-server-py/releases/tag/v0.2.0",
    );
    expect(calls[0].body.text).toContain("{{{RESEND_UNSUBSCRIBE_URL}}}");
    expect(calls[0].body.html).toContain("{{{RESEND_UNSUBSCRIBE_URL}}}");
  });

  it("handles a draft promoted with action=released", async () => {
    const secret = "whsec";
    const body = JSON.stringify({
      action: "released",
      release: { id: 124, tag_name: "v0.2.1", draft: false, prerelease: false },
    });
    const calls = installFetch(() => json({ id: "broadcast_1" }));
    const response = await worker.fetch(
      releaseRequest(body, await githubSignature(secret, body)),
      makeEnv(),
    );
    expect(response.status).toBe(200);
    expect(calls).toHaveLength(1);
  });

  it("does not broadcast twice for the same release", async () => {
    const secret = "whsec";
    const body = JSON.stringify({
      action: "published",
      release: { id: 123, tag_name: "v0.2.0", draft: false, prerelease: false },
    });
    const signature = await githubSignature(secret, body);
    const calls = installFetch(() => json({ id: "broadcast_1" }));
    const env = makeEnv();

    await worker.fetch(releaseRequest(body, signature), env);
    const replay = await worker.fetch(releaseRequest(body, signature), env);

    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({ ok: true, duplicate: true });
    expect(calls).toHaveLength(1);
  });

  it("rejects a bad signature without touching Resend", async () => {
    const body = JSON.stringify({
      action: "published",
      release: { id: 123, tag_name: "v0.2.0", draft: false, prerelease: false },
    });
    const calls = installFetch(() => json({ id: "broadcast_1" }));

    const response = await worker.fetch(
      releaseRequest(body, `sha256=${"0".repeat(64)}`),
      makeEnv(),
    );

    expect(response.status).toBe(401);
    expect(calls).toHaveLength(0);
  });

  it("ignores drafts and prereleases", async () => {
    const secret = "whsec";
    for (const flags of [{ draft: true }, { prerelease: true }]) {
      const body = JSON.stringify({
        action: "published",
        release: { id: 456, tag_name: "v0.3.0", draft: false, prerelease: false, ...flags },
      });
      const calls = installFetch(() => json({ id: "broadcast_1" }));
      const response = await worker.fetch(
        releaseRequest(body, await githubSignature(secret, body)),
        makeEnv(),
      );
      expect(response.status).toBe(204);
      expect(calls).toHaveLength(0);
    }
  });

  it("rejects a non-semver tag", async () => {
    const secret = "whsec";
    const body = JSON.stringify({
      action: "published",
      release: { id: 789, tag_name: "latest", draft: false, prerelease: false },
    });
    const calls = installFetch(() => json({ id: "broadcast_1" }));
    const response = await worker.fetch(
      releaseRequest(body, await githubSignature(secret, body)),
      makeEnv(),
    );
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(0);
  });
});
