// One-off captures for the stage-3/4/9 shell screenshots (mock fleet):
//   console-nav-admin.png    — signed-in shell, role-gated Admin section
//   console-agents-session.png — session slot: role, expiry, Extend, Sign out
//   console-login-error.png  — the 401 message on a failed login
// Run with the mock dev server up:
//   npm run dev -- --port 5199 --strictPort   (VITE_USE_MOCK_API=1)
//   node tools/screenshot-shell.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";
const NAV_OUT = "../docs/screenshots/console-nav-admin.png";
const SESSION_OUT = "../docs/screenshots/console-agents-session.png";
const LOGIN_OUT = "../docs/screenshots/console-login-error.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

// Login error: wrong password, the 401 message inline.
await page.goto(`${BASE}/login`);
await page.getByLabel("Name").fill("admin");
await page.getByLabel("Password").fill("wrong-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("alert").waitFor();
await page.waitForTimeout(250);
await page.screenshot({ path: LOGIN_OUT });
console.log(`saved ${LOGIN_OUT}`);

// Signed-in shell with the session slot and the Admin section.
await page.getByLabel("Name").fill("admin");
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(`${BASE}/`, { timeout: 60_000 });
// Wait out the session/agents queries: the shot needs the Admin section and
// the populated table, not the loading skeletons.
await page.getByRole("navigation", { name: "Admin" }).waitFor();
await page.getByRole("cell", { name: "shop-prod-collector" }).waitFor();
await page.getByText("role admin", { exact: false }).waitFor();
await page.waitForTimeout(250);
await page.screenshot({ path: NAV_OUT });
console.log(`saved ${NAV_OUT}`);
await page.screenshot({ path: SESSION_OUT });
console.log(`saved ${SESSION_OUT}`);
await browser.close();
