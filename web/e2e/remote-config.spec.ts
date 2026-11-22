import { expect, test } from "@playwright/test";

/**
 * The config-push stepper end-to-end against the stateful mock transport
 * (new-ui stage 6 done-when, mocked-API half): target → edit → validate →
 * propose → plan → approve & roll out → monitor → abort. The mock makes
 * "create rollout = approval", so the whole lifecycle is exercisable without
 * the live server; the live round-trip is stage 9's pass.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

const CONFIG_BODY = `receivers:
  otlp:
    protocols:
      grpc:
exporters:
  debug:
service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [debug]
`;

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  // Client-side navigation only: a full goto reloads the JS context and the
  // mock transport's session flag with it (real mode keeps the cookie).
  await page.getByRole("link", { name: "Remote Config" }).click();
  await expect(page.getByTestId("compose-stepper")).toBeVisible();
}

test("full stepper: propose → plan → approve → rollout → abort", async ({ page }) => {
  await login(page);

  // 1. Target: ad-hoc, select the collector.
  await page.getByLabel(/Select shop-prod-collector/).check();
  await expect(page.getByText(/1 agent\(s\) in scope/)).toBeVisible();

  // 2. Edit: paste a config body; a redacted body is refused by the UI itself.
  await page.getByLabel("Config body").fill(CONFIG_BODY);
  await expect(page.getByText(/This body contains/)).toHaveCount(0);

  // 3. Validate → verdict panel, valid.
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByTestId("verdict-panel")).toContainText("Valid —");

  // 4. Propose → proposal row appears.
  await page.getByRole("button", { name: "Propose revision" }).click();
  await expect(page.getByTestId("proposal-status")).toContainText("proposed");

  // 5. Plan the rollout, then create it (the approval).
  await page.getByRole("button", { name: "Plan the rollout (dry run)" }).click();
  await expect(page.getByText(/Rollout plan/)).toBeVisible();
  await expect(page.getByText(/1 batch\(es\), canary 1 agent/)).toBeVisible();
  await page.getByTestId("approve-rollout").click();

  // 6. Monitor: running rollout, canary batch in flight.
  await expect(page.getByText("6. Roll out")).toBeVisible();
  const monitor = page.getByText(/Batch 1\/1/);
  await expect(monitor).toBeVisible();

  // Stop: abort (a decision a dialog confirms).
  await page.getByRole("button", { name: "Abort promotion" }).click();
  await page.getByTestId("confirm-abort").click();
  await expect(page.getByText(/Aborted\. Configs already delivered were/)).toBeVisible();
});

test("redacted bodies are refused before anything is sent", async ({ page }) => {
  await login(page);
  await page.getByLabel(/Select shop-prod-collector/).check();
  await page.getByLabel("Config body").fill('exporters: <redacted>');
  await expect(page.getByText(/wipe every credential/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Validate" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Propose revision" })).toBeDisabled();
});

test("rollouts and revisions history tabs render", async ({ page }) => {
  await login(page);
  await page.getByRole("tab", { name: "Rollouts" }).click();
  await expect(page.getByText("roll-mock-old")).toBeVisible();
  await page.getByRole("tab", { name: "Revisions" }).click();
  await expect(page.getByText("rev-mock-p1")).toBeVisible();
  // The poisoned revision offers reactivate — a dialog, never an auto-retry.
  await expect(page.getByRole("button", { name: "Reactivate" })).toBeVisible();
});
