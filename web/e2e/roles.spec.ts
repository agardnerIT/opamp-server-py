import { expect, test } from "@playwright/test";

/**
 * Role-denial cases (new-ui stage 9 done-when), against the stateful mock:
 * the mock enforces the same role floor the live endpoints do, so the ladder
 * (viewer < operator < approver < admin) is exercised end to end — nav hiding
 * and disabled buttons are hints only, the mock's 403s are the authority.
 *
 * The login name selects the role (fixtures.mockPrincipals); the password is
 * the same for every role, exactly as a principal file would share none.
 */

const PASSWORD = "opamp-mock-password"; // fixtures.validPassword

/** Sign in as a named principal; lands on `/` with the session slot filled. */
async function loginAs(page: import("@playwright/test").Page, name: string) {
  await page.goto("/login");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.getByText(`role ${name}`, { exact: false })).toBeVisible();
}

test("viewer: no Admin section, mutations refused by the server, admin-only actions disabled", async ({
  page,
}) => {
  await loginAs(page, "viewer");

  // The whole Admin section is a client-side hint for lower roles.
  await expect(page.getByRole("navigation", { name: "Admin" })).toHaveCount(0);

  // Direct navigation still lands (the hint is never a router guard), and the
  // viewer-level reads succeed — GET /alerts is viewer on the server.
  await page.goto("/alerts");
  await expect(page.getByRole("heading", { name: "Alerts" })).toBeVisible();

  // Saving is operator-level: the server refuses, the session stays.
  await page.getByLabel("Body template").fill('{"viewer": true}');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(
    page.getByText(/role 'viewer' cannot perform this action \(requires 'operator'\)/),
  ).toBeVisible();
  await expect(page.getByText("role viewer", { exact: false })).toBeVisible();

  // GET /agent-tokens is viewer-level too: the page renders, the actions
  // disable with the caption naming the role actually required.
  await page.goto("/agent-tokens");
  await expect(page.getByRole("heading", { name: "Mint a token" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Mint token" })).toBeDisabled();
  await expect(page.getByText("Minting needs the admin role.")).toBeVisible();

  // GET /auth/sessions is admin-only: the honest 403, session intact.
  await page.goto("/sessions");
  await expect(
    page.getByText(/role 'viewer' cannot perform this action \(requires 'admin'\)/),
  ).toBeVisible();
});

test("operator: Admin section shows without admin-only items; alerts save; token minting still refused", async ({
  page,
}) => {
  await loginAs(page, "operator");

  const adminNav = page.getByRole("navigation", { name: "Admin" });
  await expect(adminNav).toBeVisible();
  await expect(adminNav.getByRole("link", { name: "Alerts" })).toBeVisible();
  await expect(adminNav.getByRole("link", { name: "Agent tokens" })).toHaveCount(0);
  await expect(adminNav.getByRole("link", { name: "Sessions" })).toHaveCount(0);

  // Operator-level mutation goes through.
  await page.goto("/alerts");
  await page.getByLabel("Body template").fill('{"operator": true}');
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();

  // Minting is admin: button disabled with its caption (direct navigation).
  await page.goto("/agent-tokens");
  await expect(page.getByRole("button", { name: "Mint token" })).toBeDisabled();
  await expect(page.getByText("Minting needs the admin role.")).toBeVisible();

  // Compliance policy reload is admin; the per-agent check is operator.
  await page.goto("/compliance");
  await expect(page.getByRole("button", { name: "Reload & re-validate" })).toBeDisabled();
  await page.getByLabel("Select an agent").selectOption({ label: "shop-prod-collector" });
  await page.getByRole("button", { name: "Check compliance" }).click();
  await expect(page.getByText(/Compliant|violation/)).toBeVisible();
});

test("approver: can steer a rollout but cannot see admin-only pages or list sessions", async ({
  page,
}) => {
  await loginAs(page, "approver");

  // Remote Config is the approver's page: the stepper renders and the
  // operator/approver affordances are live (nothing is disabled by role).
  await page.goto("/remote-config");
  await expect(page.getByTestId("compose-stepper")).toBeVisible();
  await page.getByRole("tab", { name: "Rollouts" }).click();
  await expect(page.getByText("roll-mock-old")).toBeVisible();

  // Admin section visible (approver ≥ operator), admin-only items still hidden.
  const adminNav = page.getByRole("navigation", { name: "Admin" });
  await expect(adminNav.getByRole("link", { name: "Alerts" })).toBeVisible();
  await expect(adminNav.getByRole("link", { name: "Agent tokens" })).toHaveCount(0);

  // Sessions: read refused by the server even though the URL is reachable.
  await page.goto("/sessions");
  await expect(
    page.getByText(/role 'approver' cannot perform this action \(requires 'admin'\)/),
  ).toBeVisible();
});
