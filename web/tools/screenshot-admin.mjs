// One-off capture for the stage-8 screenshots: the admin pages (Alerts,
// Agent tokens with a fresh mint, Sessions, Compliance, Updates). Mock fleet,
// stateful handlers.
//   npm run dev -- --port 5199 --strictPort   (VITE_USE_MOCK_API=1)
//   node tools/screenshot-admin.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Name").fill("admin");
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(`${BASE}/`, { timeout: 60_000 });

const capture = async (nav, waitForText, path) => {
  await page.getByRole("link", { name: nav }).click();
  await page.getByText(waitForText, { exact: false }).first().waitFor();
  await page.waitForTimeout(300);
  await page.screenshot({ path: `../docs/screenshots/${path}`, fullPage: true });
  console.log(`saved ../docs/screenshots/${path}`);
};

await capture("Alerts", "Webhook alerts for fleet events", "console-alerts.png");
await capture("Agent tokens", "Issued tokens", "console-agent-tokens.png");
await capture("Sessions", "Every admin session", "console-sessions.png");
await capture("Compliance", "Check an agent", "console-compliance.png");
await capture("Updates", "Release emails", "console-updates.png");

await browser.close();
