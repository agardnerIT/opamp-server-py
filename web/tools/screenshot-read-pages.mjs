// One-off capture for the stage-7 screenshots: the Groups page (search run +
// saved-groups table + legacy-group detail) and the Reports page (fleet
// summary). Mock fleet, stateful handlers.
//   npm run dev -- --port 5199 --strictPort   (VITE_USE_MOCK_API=1)
//   node tools/screenshot-read-pages.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Name").fill("admin");
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(`${BASE}/`, { timeout: 60_000 });

// Groups: run the search, then open the legacy group's detail panel.
await page.getByRole("link", { name: "Groups" }).click();
await page.getByText("1 · Search agents").waitFor();
await page.getByLabel("SQL selection").fill("SELECT * FROM agents WHERE healthy = 1");
await page.getByLabel("SQL selection").press("Enter");
await page.getByText("Agents matched:").waitFor();
await page.getByRole("cell", { name: "prod-agents" }).click();
await page.getByTestId("group-detail").waitFor();
await page.waitForTimeout(300);
await page.screenshot({ path: "../docs/screenshots/console-groups.png", fullPage: true });
console.log("saved ../docs/screenshots/console-groups.png");

// Reports: the fleet summary.
await page.getByRole("link", { name: "Reports" }).click();
await page.getByRole("heading", { name: "Agent Report" }).waitFor();
await page.waitForTimeout(200);
await page.screenshot({ path: "../docs/screenshots/console-reports.png", fullPage: true });
console.log("saved ../docs/screenshots/console-reports.png");

await browser.close();
