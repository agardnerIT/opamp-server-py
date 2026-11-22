/**
 * Auth-flow tests against the mock transport (same code paths as live):
 * login success strips the token, 401/429 classification, logout, and the
 * global unauthorized handler contract.
 */

import { afterEach, describe, expect, it } from "vitest";
import {
  extendSession,
  getMe,
  listAgents,
  login,
  logout,
} from "./endpoints";
import { classifyError, setUnauthorizedHandler } from "./errors";
import { fixtures } from "./fixtures";

// vitest.config.ts sets VITE_USE_MOCK_API=1 before any module init, so the
// client under test is bound to mockFetch — no network, no live server.

const CORRECT = fixtures.validPassword;

afterEach(() => {
  setUnauthorizedHandler(() => undefined);
});

describe("login", () => {
  it("succeeds and never exposes the token to the caller", async () => {
    const result = await login("", CORRECT);
    expect(result.principal).toEqual({ name: "admin", role: "admin" });
    expect(result.expires_at).toBeTruthy();
    expect("token" in result).toBe(false);
    expect("token_type" in result).toBe(false);
  });

  it("wrong credentials are 401 and mean relogin — but carry no session claim", async () => {
    let redirected = 0;
    setUnauthorizedHandler(() => {
      redirected += 1;
    });
    await expect(login("", "wrong")).rejects.toMatchObject({
      status: 401,
      kind: "unauthorized",
      relogin: true,
    });
    // The handler fired; main.tsx's guard is what keeps a failed login form
    // from routing (the handler itself stays location-agnostic).
    expect(redirected).toBe(1);
  });

  it("identity follows from the cookie, not returned state", async () => {
    await login("", CORRECT);
    const me = await getMe();
    expect(me.authenticated).toBe(true);
    expect(me.method).toBe("session");
  });

  it("signed-out identity answers 401", async () => {
    await logout();
    await expect(getMe()).rejects.toMatchObject({ status: 401 });
  });
});

describe("logout", () => {
  it("revokes the session and clears the cookie state", async () => {
    await login("", CORRECT);
    const result = await logout();
    expect(result.logged_out).toBe(true);
    await expect(getMe()).rejects.toMatchObject({ status: 401 });
  });

  it("is idempotent — already signed out reports logged_out false", async () => {
    await logout();
    const result = await logout();
    expect(result.logged_out).toBe(false);
  });
});

describe("extendSession", () => {
  it("revokes the old session, then mints a new one (audited re-auth)", async () => {
    await login("", CORRECT);
    const result = await extendSession("", CORRECT);
    expect("token" in result).toBe(false);
    const me = await getMe();
    expect(me.authenticated).toBe(true);
  });

  it("if the fresh login fails, the old session is already gone", async () => {
    await login("", CORRECT);
    await expect(extendSession("", "wrong")).rejects.toMatchObject({
      status: 401,
    });
    await expect(getMe()).rejects.toMatchObject({ status: 401 });
  });
});

describe("401 classification is sacred (plan rule 4)", () => {
  it("unauthorized means relogin, never a permissions wording", () => {
    const error = classifyError(
      new Response(null, { status: 401 }),
      { detail: "invalid credentials" },
    );
    expect(error.relogin).toBe(true);
    expect(error.message).not.toMatch(/not allowed|permission|forbidden/i);
  });
});

describe("other endpoints keep working through the same client", () => {
  it("agents list renders through the typed wrapper", async () => {
    const payload = await listAgents();
    expect(payload.count).toBe(2);
  });
});
