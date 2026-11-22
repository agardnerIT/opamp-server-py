// One-off capture for the zero-agent landing page (Overview quickstart +
// fleet graphic), in both states. Uses the mock's empty-fleet switch, which
// `GET /agents` honours from localStorage (see `src/api/mock.ts`):
//   VITE_USE_MOCK_API=1 npm run dev -- --port 5199 --strictPort
//   node tools/screenshot-quickstart.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.addInitScript(() => localStorage.setItem("opamp-mock-empty-fleet", "1"));
await page.goto(`${BASE}/login`);
await page.getByLabel("Name").fill("admin");
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(`${BASE}/`, { timeout: 60_000 });

await page.getByRole("heading", { name: "Connect your first agent" }).waitFor();
await page.waitForTimeout(400);
await page.screenshot({ path: "../docs/screenshots/console-overview-empty.png", fullPage: true });
console.log("saved ../docs/screenshots/console-overview-empty.png");

// Mint a token: the page now shows the once-only credential + snippet + the
// waiting state, which is the screen an operator is looking at when their
// first agent arrives.
await page.getByLabel("Label").fill("prod");
await page.getByRole("button", { name: "Generate API token" }).click();
await page.getByText("Copy it now", { exact: false }).waitFor();
await page.waitForTimeout(400);
await page.screenshot({ path: "../docs/screenshots/console-overview-token.png", fullPage: true });
console.log("saved ../docs/screenshots/console-overview-token.png");

// Flip to a connected fleet and let the poll (or the button) re-read it, so the
// connected hero — a drawn line per agent — is captured too.
await page.evaluate(() => localStorage.removeItem("opamp-mock-empty-fleet"));
await page.getByRole("button", { name: "Check again" }).click();
await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
await page.waitForTimeout(900); // let the connectors finish drawing
await page.screenshot({ path: "../docs/screenshots/console-overview.png", fullPage: true });
console.log("saved ../docs/screenshots/console-overview.png");

await browser.close();
