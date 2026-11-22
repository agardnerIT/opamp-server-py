// One-off captures for the stage-5 screenshots: the agents list (mock fleet,
// two agent types) and the collector agent detail with profile-declared
// telemetry panels. Run with the mock dev server up:
//   npm run dev -- --port 5199 --strictPort   (VITE_USE_MOCK_API=1)
//   node tools/screenshot-agents.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";
const LIST_OUT = "../docs/screenshots/console-agents-list.png";
const DETAIL_OUT = "../docs/screenshots/console-agent-detail.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
// First visit compiles Vite deps; the login POST itself settles quickly, so
// give the redirect a generous window and the shell time to hydrate.
await page.waitForURL(`${BASE}/`, { timeout: 60_000 });
await page.waitForTimeout(500);
// Wait out the session/agents/schema queries: the shot needs the populated
// table with both agent types, not the loading states.
await page.getByRole("cell", { name: "shop-prod-collector" }).waitFor();
await page.getByRole("cell", { name: "Generic agent" }).waitFor();
await page.waitForTimeout(250);
await page.screenshot({ path: LIST_OUT });
console.log(`saved ${LIST_OUT}`);

// Collector detail: type-aware telemetry panels + component inventory.
await page.getByRole("link", { name: "shop-prod-collector" }).click();
await page.getByText("Data Flow (Inputs)").waitFor();
await page.getByRole("cell", { name: "otlp", exact: true }).waitFor();
await page.waitForTimeout(250);
await page.screenshot({ path: DETAIL_OUT, fullPage: true });
console.log(`saved ${DETAIL_OUT}`);
await browser.close();
