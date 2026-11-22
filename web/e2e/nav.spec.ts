import { expect, test } from "@playwright/test";

/**
 * Navigation parity (new-ui stage 4 done-when): every Streamlit page has a
 * reachable route, the Admin section appears for a role that can use it, and
 * unknown URLs land on the not-found page. Mock transport; the mock session
 * is admin, so all admin items should be visible.
 */

const CORRECT = "opamp-mock-password"; // fixtures.validPassword

/** Every shell route with its page title (flat URLs, like Streamlit's). */
const ROUTES = [
  ["/agents", "Agents"],
  ["/reports", "Reports"],
  ["/groups", "Groups"],
  ["/remote-config", "Remote Config"],
  ["/help", "Help"],
  ["/alerts", "Alerts"],
  ["/compliance", "Compliance"],
  ["/agent-tokens", "Agent tokens"],
  ["/sessions", "Sessions"],
  ["/updates", "Updates"],
] as const;

test("signed in as admin: Admin section visible, every page reachable", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");

  const adminNav = page.getByRole("navigation", { name: "Admin" });
  await expect(adminNav).toBeVisible();
  // Admin-only items visible to the admin role.
  await expect(adminNav.getByRole("link", { name: "Agent tokens" })).toBeVisible();
  await expect(adminNav.getByRole("link", { name: "Sessions" })).toBeVisible();

  for (const [path, title] of ROUTES) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { level: 1, name: title }),
    ).toBeVisible();
  }

  // '/' is the Overview dashboard now (Stage B), and it marks itself too.
  await page.goto("/");
  await expect(
    page.getByRole("heading", { level: 1, name: "Overview" }),
  ).toBeVisible();
});

test("Overview is the only page marked active on '/', and each page marks itself", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");

  const mainNav = page.getByRole("navigation", { name: "Main" });
  await expect(
    mainNav.getByRole("link", { name: "Overview", exact: true }),
  ).toHaveAttribute("aria-current", "page");

  await page.goto("/agents");
  await expect(
    mainNav.getByRole("link", { name: "Agents", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(
    mainNav.getByRole("link", { name: "Overview", exact: true }),
  ).not.toHaveAttribute("aria-current", "page");

  await page.goto("/groups");
  await expect(
    mainNav.getByRole("link", { name: "Groups" }),
  ).toHaveAttribute("aria-current", "page");
});

test("the Overview dashboard shows the fleet hero and activity strip", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();

  // Hero: counts derived from GET /agents (mock fleet: 2 agents, 1 healthy).
  // exact: the page meta line "2 agents connected" contains the same words.
  await expect(page.getByText("Agents connected", { exact: true })).toBeVisible();
  await expect(page.getByText("Agents by type")).toBeVisible();
  // The fleet graphic above the counts draws one line per agent (the zero-agent
  // variant has its own spec: `quickstart.spec.ts`).
  const graphic = page.getByRole("group", { name: /agents connected to the server/i });
  await expect(graphic).toBeVisible();
  await expect(graphic.getByText("shop-prod-collector")).toBeVisible();
  await expect(page.getByText("Each line is a live OpAMP session.")).toBeVisible();
  // Every chip carries its heartbeat age (compact), live agents included.
  await expect(graphic.getByTitle(/^Last heartbeat /)).toHaveCount(2);
  // Activity strip links through to the underlying pages.
  await expect(page.getByText("Rollouts in progress")).toBeVisible();
  await expect(page.getByText("Alert events armed")).toBeVisible();
  // Entry cards route onward.
  await page.getByRole("link", { name: /Reports.*fleet analysis/s }).click();
  await expect(page).toHaveURL("/reports");
});

test("unknown URLs render the not-found page", async ({ page }) => {
  // Signed in first: an *anonymous* visitor who follows "Back to Overview" is
  // bounced to /login by the 401 handler, which made this assertion a race
  // (the URL is "/" for one tick). The link's destination is what is under
  // test, so give the session that can stay on it.
  await page.goto("/login");
  await page.getByLabel("Name").fill("admin");
  await page.getByLabel("Password").fill(CORRECT);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");

  await page.goto("/definitely-not-a-page");
  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Back to Overview" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
});
