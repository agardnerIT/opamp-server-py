import { describe, expect, it } from "vitest";
import { ApiError, classifyError, retryAfterSeconds, unwrap } from "./errors";

function response(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("classifyError", () => {
  it("401 means the session is dead — relogin, never a permissions wording", () => {
    const err = classifyError(response(401, { detail: "session expired" }), { detail: "session expired" });
    expect(err).toBeInstanceOf(ApiError);
    expect(err.kind).toBe("unauthorized");
    expect(err.relogin).toBe(true);
    expect(err.message).toMatch(/sign in again/i);
  });

  it("403 keeps the session and names the refusal", () => {
    const err = classifyError(
      response(403, { detail: { message: "requires the approver role" } }),
      { detail: { message: "requires the approver role" } },
    );
    expect(err.relogin).toBe(false);
    expect(err.kind).toBe("forbidden");
    expect(err.message).toContain("approver");
  });

  it("403 with the read-only hint is the no-credential case, not a role problem", () => {
    const err = classifyError(
      response(403, { detail: "this server is read-only" }),
      { detail: "this server is read-only" },
    );
    expect(err.kind).toBe("read-only");
    expect(err.message).toMatch(/no admin credential/i);
  });

  it("429 prefers the Retry-After header, then the payload, then 30s", () => {
    const headerErr = classifyError(
      response(429, {}, { "Retry-After": "120" }),
      {},
    );
    expect(headerErr.kind).toBe("locked-out");
    expect(headerErr.retryAfterSeconds).toBe(120);

    const payloadErr = classifyError(
      response(429, { retry_after: 45 }),
      { retry_after: 45 },
    );
    expect(payloadErr.retryAfterSeconds).toBe(45);

    const fallbackErr = classifyError(response(429, {}), {});
    expect(fallbackErr.retryAfterSeconds).toBe(30);
  });

  it("503 is the audit-store refusal unless the server said something specific", () => {
    const err = classifyError(response(503, {}), {});
    expect(err.kind).toBe("audit-unavailable");
    expect(err.message).toMatch(/audit log is unavailable/i);

    const detailed = classifyError(
      response(503, { detail: "audit store offline" }),
      { detail: "audit store offline" },
    );
    expect(detailed.message).toBe("audit store offline");
  });

  it("other statuses fall back to client/server kinds with the body text", () => {
    expect(classifyError(response(400, { detail: "bad query" }), { detail: "bad query" }).kind).toBe("client");
    expect(classifyError(response(404, {}), {}).message).toContain("404");
    expect(classifyError(response(500, {}), {}).kind).toBe("server");
  });
});

describe("retryAfterSeconds", () => {
  it("parses Retry-After", () => {
    const res = response(429, {}, { "Retry-After": "7" });
    expect(retryAfterSeconds(res, undefined)).toBe(7);
  });

  it("handles a stripped header (proxy) with the payload fallback", () => {
    const res = response(429, { retry_after_seconds: 12 });
    expect(retryAfterSeconds(res, { retry_after_seconds: 12 })).toBe(12);
  });
});

describe("unwrap", () => {
  it("throws ApiError on an error response", async () => {
    const res = response(403, { detail: "no" });
    const body = await res.clone().json();
    await expect(
      unwrap(Promise.resolve({ error: body, response: res })),
    ).rejects.toMatchObject({ kind: "forbidden", status: 403 });
  });

  it("resolves undefined as a success when the body is empty", async () => {
    const res = new Response(null, { status: 204 });
    await expect(
      unwrap(Promise.resolve({ response: res })),
    ).resolves.toBeUndefined();
  });

  it("throws when the response is not ok and no error was parsed", async () => {
    const res = response(500, "");
    await expect(
      unwrap(Promise.resolve({ response: res })),
    ).rejects.toMatchObject({ kind: "server" });
  });
});
