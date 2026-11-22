import { expect, test } from "@playwright/test";

/**
 * The config-push stepper end-to-end against the stateful mock transport:
 * target → edit → validate → push → monitor → abort. The push is one action
 * (no proposal step, no dry run); the mock keeps the same approval semantics,
 * so the whole lifecycle is exercisable without the live server.
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
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Remote Config" }).click();
  await expect(page.getByTestId("compose-stepper")).toBeVisible();
}

test("full stepper: validate → push → rollout → abort", async ({ page }) => {
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

  // 4. Push: validate + activate + stage in one action.
  await page.getByTestId("push-config").click();

  // 5. Monitor: running rollout, canary batch in flight.
  await expect(page.getByText("5. Roll out")).toBeVisible();
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
  await expect(page.getByTestId("push-config")).toBeDisabled();
});

test("switching to a saved group does not throw", async ({ page }) => {
  // Regression: `targetIds()` read `selectedGroup` during render before its
  // declaration — a TDZ ReferenceError the moment the saved-group mode opened.
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await login(page);
  await page.getByRole("button", { name: "Saved group" }).click();
  await page.getByLabel("Saved group").selectOption({ index: 1 });
  await expect(page.getByText(/agent\(s\) in scope/)).toBeVisible();
  expect(failures).toEqual([]);
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
