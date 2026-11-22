// One-off capture for the stage-4 screenshot: the signed-in shell with the
// role-gated Admin section (mock fleet). Run with the mock dev server up:
//   npm run dev -- --port 5199 --strictPort   (VITE_USE_MOCK_API=1)
//   node tools/screenshot-shell.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";
const OUT = "../docs/screenshots/console-nav-admin.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(`${BASE}/`);
// Wait out the session/agents queries: the shot needs the Admin section and
// the populated table, not the loading skeletons.
await page.getByRole("navigation", { name: "Admin" }).waitFor();
await page.getByRole("cell", { name: "shop-prod-collector" }).waitFor();
await page.waitForTimeout(250);
await page.screenshot({ path: OUT });
console.log(`saved ${OUT}`);
await browser.close();
