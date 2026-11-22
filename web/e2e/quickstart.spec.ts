import { expect, test } from "@playwright/test";

/**
 * Zero-agent quickstart (owner, 2027-01): "when there are no agents connected
 * I want generate API token right there".
 *
 * The empty fleet comes from the mock's `opamp-mock-empty-fleet` localStorage
 * switch (`src/api/mock.ts`), so the whole journey runs against the one mock
 * dev server: Overview → mint inline → token + snippet → first agent appears →
 * the graphic draws its connection and the page becomes the connected hero.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("opamp-mock-empty-fleet", "1"));
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
});

test("an empty fleet lands on the quickstart with the token form inline", async ({ page }) => {
  await expect(page.getByRole("heading", { level: 1, name: "Connect your first agent" })).toBeVisible();

  // The graphic says what the state is, and is described for screen readers.
  const graphic = page.getByRole("group", { name: /no agents connected/i });
  await expect(graphic).toBeVisible();
  await expect(graphic.getByText("Server", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("No agents connected — the server is listening.")).toBeVisible();

  // The point of the page: mint without leaving it — and the expiry default
  // is named, not implied.
  await expect(page.getByRole("button", { name: "Generate API token" })).toBeVisible();
  await expect(page.getByLabel("Label")).toBeVisible();
  await expect(page.getByLabel("Agent type")).toBeVisible();
  const expires = page.getByLabel("Expires");
  await expect(expires).toHaveValue("");
  await expect(expires.getByRole("option", { name: "Never (default)" })).toHaveCount(1);
  await expect(page.getByText("No expiry: the token works until you revoke it.")).toBeVisible();
});

test("the expiry choice is sent and shown back on the minted token", async ({ page }) => {
  await page.getByLabel("Expires").selectOption("30");
  await expect(page.getByText("This token stops working in 30 days.")).toBeVisible();
  await page.getByRole("button", { name: "Generate API token" }).click();
  await expect(page.getByText("Copy it now — it is not shown again.")).toBeVisible();
  // The mock honours `expires_in_days`: the minted card states the date rather
  // than leaving the operator to remember what they picked.
  await expect(page.getByText(/Expires \d{4}-\d{2}-\d{2}\./)).toBeVisible();
});

test("minting shows the token once, with its snippet and a live wait", async ({ page }) => {
  await page.getByLabel("Label").fill("prod");
  await page.getByLabel("Agent type").selectOption("otelcol");
  await page.getByRole("button", { name: "Generate API token" }).click();

  await expect(page.getByText("Copy it now — it is not shown again.")).toBeVisible();
  await expect(page.getByText(/not-a-real-credential/)).toBeVisible();
  // Copy affordances for both the credential and the paste-ready snippet.
  await expect(page.getByRole("button", { name: "Copy token" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy snippet" })).toBeVisible();
  await expect(page.getByText("Waiting for the first agent to check in…")).toBeVisible();
  // The form is replaced, not stacked — one thing to do at a time.
  await expect(page.getByRole("button", { name: "Generate API token" })).toHaveCount(0);
});

test("the first agent to connect replaces the quickstart with the connected hero", async ({ page }) => {
  await page.getByRole("button", { name: "Generate API token" }).click();
  await expect(page.getByText("Waiting for the first agent to check in…")).toBeVisible();

  // Flip the fleet back on and re-read it — this is the transition the owner
  // asked for: agents appear, with a drawn line to the server.
  await page.evaluate(() => localStorage.removeItem("opamp-mock-empty-fleet"));
  await page.getByRole("button", { name: "Check again" }).click();

  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  const graphic = page.getByRole("group", { name: /2 agents connected to the server/i });
  await expect(graphic).toBeVisible();
  await expect(graphic.getByText("shop-prod-collector")).toBeVisible();
  await expect(page.getByText("Each line is a live OpAMP session.")).toBeVisible();
  // The once-only credential is gone with the panel it lived in.
  await expect(page.getByText(/not-a-real-credential/)).toHaveCount(0);
});
