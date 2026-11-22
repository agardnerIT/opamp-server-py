import { expect, test } from "@playwright/test";

/**
 * Login failure/success round-trip (new-ui stage 3 done-when), against the
 * mock transport: wrong password → 401 message, correct password → shell with
 * the session slot filled, sign out → back to login.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

test("wrong password shows the 401 message and stays on /login", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Incorrect name or password.");
  expect(new URL(page.url()).pathname).toBe("/login");
});

test("login → shell → logout round-trip", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();

  // Redirected to the console; the session slot shows identity + role.
  await expect(page).toHaveURL("/");
  const slot = page.getByText("role admin", { exact: false });
  await expect(slot).toBeVisible();
  await expect(page.getByText("shop-prod-collector")).toBeVisible();

  // Sign out → cookie revoked → back to the login page.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
});
