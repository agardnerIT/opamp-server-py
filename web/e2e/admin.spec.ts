import { expect, test } from "@playwright/test";

/**
 * Admin pages (new-ui stage 8): the mutations that motivated the redesign —
 * mint/revoke tokens, revoke a session, save + test alerts, check compliance,
 * toggle update checks — exercised against the stateful mock.
 *
 * Stage 9 owns the role-denial cases and a live-server pass.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
});

test("alerts: event fields render, saving reports success", async ({ page }) => {
  await page.goto("/alerts");
  await expect(page.getByRole("heading", { name: "Alerts" })).toBeVisible();

  // The redacted webhook arrives masked — rendering the sentinel is correct.
  const url = page.getByLabel("Webhook URL");
  await expect(url).toHaveValue(/REDACTED/);

  await page.getByLabel("Body template").fill('{"custom": true}');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();

  await page.getByRole("button", { name: "Send test" }).click();
  await expect(page.getByText("Test alert sent.")).toBeVisible();

  // Switching events reseeds the form from the server config.
  await page.getByLabel("Event").selectOption("agent_disconnected");
  await expect(url).not.toHaveValue(/REDACTED/);
});

test("agent tokens: mint shows the token once, revoke asks for confirmation", async ({
  page,
}) => {
  await page.goto("/agent-tokens");
  await expect(page.getByRole("heading", { name: "Mint a token" })).toBeVisible();

  await page.getByLabel("Label").fill("e2e fleet");
  await page.getByRole("button", { name: "Mint token" }).click();
  await expect(page.getByText("Copy it now")).toBeVisible();
  await expect(page.getByText(/not-a-real-credential/)).toBeVisible();

  const row = page.getByRole("row", { name: /e2e fleet/ });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Revoke" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Revoke token" }).click();
  await expect(dialog).toBeHidden();
  // The State column comes first: "revoked e2e fleet …".
  await expect(page.getByRole("row", { name: /revoked.*e2e fleet/ })).toBeVisible();
});

test("sessions: revoke removes the row", async ({ page }) => {
  await page.goto("/sessions");
  const row = page.getByRole("row", { name: /admin \(shared password\)/ });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Revoke" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Revoke session" }).click();
  await expect(dialog).toBeHidden();
  await expect(row).toHaveCount(0);
  // The other (expired) session stays.
  await expect(page.getByRole("row", { name: /alice/ })).toBeVisible();
});

test("compliance: policy validation and a per-agent check", async ({ page }) => {
  await page.goto("/compliance");
  await expect(page.getByText(/1 valid, 1 invalid/)).toBeVisible();
  await expect(page.getByText("broken-policy.rego")).toBeVisible();
  await expect(page.getByText("Missing package declaration")).toBeVisible();

  const reload = page.getByRole("button", { name: "Reload & re-validate" });
  await expect(reload).toBeEnabled();

  await page.getByLabel("Select an agent").selectOption({ label: "shop-prod-collector" });
  await page.getByRole("button", { name: "Check compliance" }).click();
  await expect(page.getByText(/Compliant|violation/)).toBeVisible();
});

test("updates: status line, toggle off, check now", async ({ page }) => {
  await page.goto("/updates");
  await expect(page.getByText("update available")).toBeVisible();

  await page.getByRole("button", { name: "Check now" }).click();
  // The mock never fails a check; the status line stays.
  await expect(page.getByText("update available")).toBeVisible();

  const toggle = page.getByRole("switch", { name: "Check for updates automatically" });
  await toggle.click();
  // Controlled switch + server round-trip: the effective state flips to off.
  await expect(page.getByText("Automatic checks")).toBeVisible();
  await expect(toggle).not.toBeChecked();

  // Release emails are folded in here (no separate page since stage 8).
  await expect(page.getByText("Release emails")).toBeVisible();
  await expect(page.getByRole("link", { name: "GitHub releases" })).toBeVisible();
});
