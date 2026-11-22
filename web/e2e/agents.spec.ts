import { expect, test } from "@playwright/test";

/**
 * Agents list + detail (new-ui stage 5 done-when), against the mock transport:
 * the list renders a two-type fleet with server-side selection (SQL search,
 * health chips), and the detail view adapts to the agent's type — the
 * collector gets profile-declared telemetry panels and a component inventory,
 * the generic agent gets neither plus the honest "no telemetry" fact.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("cell", { name: "shop-prod-collector" })).toBeVisible();
});

test("list shows both agent types with type + provenance", async ({ page }) => {
  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  // Types come from the profile, with the resolution source beside them.
  await expect(table.getByText("OpenTelemetry Collector")).toBeVisible();
  await expect(table.getByText("Generic agent")).toBeVisible();
  await expect(table.getByText("sniffed", { exact: true })).toBeVisible();
  await expect(table.getByText("pinned", { exact: true })).toBeVisible();
});

test("health chips filter server-side via the `healthy` parameter", async ({ page }) => {
  await page.getByRole("button", { name: "Healthy", exact: true }).click();
  await expect(page.getByRole("cell", { name: "shop-prod-collector" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "lab-generic-forwarder" })).toHaveCount(0);
  await expect(page.getByText("Filtering by health (server-side).")).toBeVisible();
});

test("SQL search selects server-side and lives in the URL", async ({ page }) => {
  const box = page.getByLabel("Search agents with SQL");
  await box.fill("SELECT * FROM agents WHERE healthy = 0");
  await box.press("Enter");

  await expect(page).toHaveURL(/\/\?q=SELECT/);
  await expect(page.getByRole("cell", { name: "lab-generic-forwarder" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "shop-prod-collector" })).toHaveCount(0);
  await expect(page.getByText("Selection:")).toBeVisible();

  // The selection rides into the detail page and back (#146 parity).
  await page.getByRole("link", { name: "lab-generic-forwarder" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "lab-generic-forwarder" })).toBeVisible();
  await page.getByRole("link", { name: "← All agents" }).click();
  await expect(page).toHaveURL(/q=SELECT/);
  await expect(page.getByRole("cell", { name: "lab-generic-forwarder" })).toBeVisible();
});

test("suggestions come from the schema and Tab accepts one", async ({ page }) => {
  const box = page.getByLabel("Search agents with SQL");
  await box.pressSequentially("SELECT * FROM agents WHERE dep");
  // The quoted spelling for a dotted attribute — what SQLite actually needs.
  const listbox = page.getByRole("listbox");
  await expect(
    listbox.getByRole("option", { name: /deployment\.environment/ }),
  ).toBeVisible();
  await box.press("Tab");
  await expect(box).toHaveValue('SELECT * FROM agents WHERE "deployment.environment" ');
});

test("collector detail shows profile-declared telemetry panels and components", async ({
  page,
}) => {
  await page.getByRole("link", { name: "shop-prod-collector" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "shop-prod-collector" }),
  ).toBeVisible();

  // Full id on the page (the operator copies it into opampctl).
  await expect(
    page.getByText("6b1f3a4c-9d2e-4f70-8a1b-2c3d4e5f6071"),
  ).toBeVisible();

  // Profile-declared blocks, not hard-coded collector tiles: the panel titles
  // and labels come from GET /agent/types (fixtures mirror the server's YAML).
  await expect(page.getByText("Data Flow (Inputs)")).toBeVisible();
  await expect(page.getByText("Logs In", { exact: false })).toBeVisible();
  await expect(page.getByText("Collector Health")).toBeVisible();
  await expect(page.getByText("Collector Telemetry")).toBeVisible();

  // Component inventory (profile.has_components).
  const components = page.getByRole("table");
  await expect(components.getByRole("cell", { name: "otlp", exact: true })).toBeVisible();
  await expect(components.getByText("prometheus")).toBeVisible();
  // The toggle lives in the section header, not inside the table.
  await page.getByText("In use only").click();
  await expect(components.getByText("prometheus")).toHaveCount(0);

  // Redaction contract: masked config values render opaque, flagged as masked.
  await expect(page.getByText("Effective config")).toBeVisible();
  await expect(page.getByText("The server masked values it considers secret")).toBeVisible();
});

test("generic detail says the honest facts instead of empty collector panels", async ({
  page,
}) => {
  await page.getByRole("link", { name: "lab-generic-forwarder" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "lab-generic-forwarder" }),
  ).toBeVisible();

  await expect(page.getByText("This profile does not report telemetry to the server.")).toBeVisible();
  await expect(page.getByText("Remote config & delivery")).toBeVisible();
  await expect(page.getByText("apply failed: unknown receiver type")).toBeVisible();
  // No collector-only sections for a profile without the concept.
  await expect(page.getByText("Data Flow (Inputs)")).toHaveCount(0);
});
