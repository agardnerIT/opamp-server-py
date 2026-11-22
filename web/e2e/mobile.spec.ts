import { expect, test } from "@playwright/test";

/**
 * Mobile shell (2027-ui Stage B3): the owner's "no native app, the UI must
 * work on a phone" ask. Runs at 390×844 (iPhone-class width) against the
 * mock: the sidebar becomes a slide-over drawer behind a top-bar menu
 * button, the drawer's labelled navs navigate and close, and the agents
 * table scrolls horizontally instead of clipping.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

test.use({ viewport: { width: 390, height: 844 } });

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
});

test("the drawer opens from the top bar, navigates, and closes", async ({ page }) => {
  const menu = page.getByRole("button", { name: "Open navigation" });
  await expect(menu).toBeVisible();
  await menu.click();

  // Same labelled navs as the desktop sidebar — queried by name everywhere.
  const drawer = page.getByRole("complementary", { name: "Navigation" });
  await expect(drawer.getByRole("navigation", { name: "Main" })).toBeVisible();
  await expect(drawer.getByRole("navigation", { name: "Admin" })).toBeVisible();

  await drawer.getByRole("link", { name: "Agents" }).click();
  await expect(page).toHaveURL("/agents");
  // Navigating closes the drawer.
  await expect(page.getByRole("button", { name: "Open navigation" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Close navigation" })).toHaveCount(0);
});

test("Esc closes the drawer", async ({ page }) => {
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("complementary", { name: "Navigation" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Open navigation" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Close navigation" })).toHaveCount(0);
});

test("the agents table scrolls horizontally on a phone", async ({ page }) => {
  // Drawer first — on a phone the navs live inside the slide-over.
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("complementary", { name: "Navigation" }).getByRole("link", { name: "Agents" }).click();
  await expect(page.getByRole("cell", { name: "shop-prod-collector" })).toBeVisible();

  // The card's scroll wrapper stays scrollable (no overflow trap): content
  // wider than the viewport scrolls, it is not cut off.
  const wrapper = page.getByRole("table").locator("..");
  await expect(wrapper).toHaveCSS("overflow-x", "auto");
  const table = page.getByRole("table");
  expect((await table.boundingBox())!.width).toBeGreaterThan(390);
});
