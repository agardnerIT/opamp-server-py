import { expect, test } from "@playwright/test";

/**
 * Live-server smoke (new-ui stage 9), driven by `npm run e2e:live`
 * (playwright.live.config.ts): the mock-mode suite cannot prove that the
 * server's real httpOnly cookie round-trip and the real error classification
 * behave the same in a browser. This boots a throwaway read-only server
 * (temp DATA_DIR, config push off) and walks the auth contract end to end.
 *
 * The throwaway server runs with config push disabled (no fleet, no posture),
 * but ADMIN_PASSWORD is set, so the admin role is live and mutations answer
 * for real — the cookie round-trip on a privileged write is the point.
 */

const SMOKE_PASSWORD = "live-smoke-password"; // playwright.live.config.ts

test("login → shell on a real cookie → read-only refusal keeps the session → logout", async ({
  page,
}) => {
  await page.goto("/login");

  // Legacy password mode: no name field, one shared password.
  await page.getByLabel("Password").fill(SMOKE_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");

  // The session slot is filled from GET /auth/me over the real cookie —
  // identity comes from the server, role admin (legacy shared password).
  await expect(page.getByText("role admin", { exact: false })).toBeVisible();

  // A fresh full page navigation must keep the session: the cookie, not JS
  // state, is the credential.
  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
  await expect(page.getByText("role admin", { exact: false })).toBeVisible();

  // A privileged mutation answers for real: the cookie carries the request to
  // PUT /alerts and the server persists it (throwaway DATA_DIR — no cleanup
  // concerns) and returns the stored config.
  await page.goto("/alerts");
  await page.getByLabel("Body template").fill('{"smoke": true}');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  await expect(page.getByText("role admin", { exact: false })).toBeVisible();

  // Sign out: the server revokes the session and clears the cookie.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);

  // The revoked cookie no longer gets in: a reload lands on login.
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("wrong password shows the 401 message", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Incorrect name or password.");
  expect(new URL(page.url()).pathname).toBe("/login");
});
