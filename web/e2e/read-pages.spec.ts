import { expect, test } from "@playwright/test";

/**
 * Stage-7 read pages (Reports, Groups, Help) against the stateful mock
 * transport. Group mutations run in one test: the mock's group state is
 * module-scope and survives within a worker, so the create/edit/delete
 * round-trip must not fight itself across tests.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill("opamp-mock-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL("/");
});

test("Reports renders the fleet summary and offers the profiles' reports", async ({ page }) => {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Reports" }).click();
  await page.getByRole("heading", { name: "Agent Report" }).waitFor();
  await expect(page.getByText("Full fleet analysis — 2 agent(s)")).toBeVisible();

  // The mock fleet's otelcol profile declares heavy + outdated; the generic
  // one declares nothing — the union, in stable order, is what the select shows.
  await page.getByLabel("Report type").selectOption({ label: "Heavy agents (>50% unused components)" });
  await page.getByRole("heading", { name: "Heavy Collectors Report" }).waitFor();
  await expect(page.getByText("not applicable: 1 × generic")).toBeVisible();

  await page.getByLabel("Report type").selectOption({ label: "Outdated components" });
  await expect(page.getByLabel("Minimum version")).toBeVisible();
  await page.getByRole("heading", { name: "Outdated Collectors Report" }).waitFor();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Markdown" }).click();
  expect((await download).suggestedFilename()).toMatch(/\.md$/);
});

test("Groups runs a saved selection, saves it as a group, previews and deletes", async ({ page }) => {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Groups" }).click();
  await page.getByText("1 · Search agents").waitFor();

  // Run the default editor query: mock selection resolves healthy = 1.
  await page.getByLabel("SQL selection").fill("SELECT * FROM agents WHERE healthy = 1");
  await page.getByLabel("SQL selection").press("Enter");
  await page.getByText("Agents matched:").waitFor();
  await expect(page.getByRole("link", { name: "shop-prod-collector" })).toBeVisible();

  // Save the query as a group (create form carries the editor text).
  await page.getByPlaceholder("macos-au").fill("e2e-group");
  await page.getByRole("button", { name: "Save group" }).click();
  await page.getByText(/Saved e2e-group with 1 member/).waitFor();
  await expect(page.getByRole("cell", { name: "e2e-group" })).toBeVisible();

  // Inspect the legacy fixture group: definition kind, unknown pin, drift count.
  await page.getByRole("cell", { name: "prod-agents" }).click();
  await page.getByTestId("group-detail").waitFor();
  await expect(page.getByText("Legacy filter-document group (no SQL query yet)")).toBeVisible();
  await expect(page.getByText("Pins for agents that have never reported: deadbeef0000")).toBeVisible();

  // Preview the SQL group; then delete it and watch the name free up.
  await page.getByRole("cell", { name: "e2e-group" }).click();
  await page.getByRole("button", { name: "Preview membership" }).click();
  await page.getByTestId("group-preview").waitFor();
  await page.getByRole("button", { name: "Delete group" }).click();
  await page.getByRole("button", { name: "Delete group", exact: true }).last().click();
  await page.getByText(/Deleted e2e-group — the name is free for reuse/).waitFor();
  await expect(page.getByRole("cell", { name: "e2e-group" })).toHaveCount(0);
});

test("Help describes the profiles from the dictionary and links the query guide", async ({ page }) => {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Help" }).click();
  await page.getByText("Known agent types").waitFor();
  await page.getByText("OpenTelemetry Collector").click();
  await expect(page.getByText("Component inventory: yes")).toBeVisible();
  await expect(page.getByText("docs/sql-agent-queries.md")).toBeVisible();
  await expect(page.getByRole("link", { name: "Admin → Agent tokens" })).toHaveAttribute("href", /agent-tokens/);
});
