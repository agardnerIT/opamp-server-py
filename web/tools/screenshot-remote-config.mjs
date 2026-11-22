// One-off capture for the stage-6 screenshot: the Remote Config stepper after
// a proposal exists, plus the rollout monitor (mock fleet, stateful handlers).
//   npm run dev -- --port 5199 --strictPort   (VITE_USE_MOCK_API=1)
//   node tools/screenshot-remote-config.mjs
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5199";
const OUT = "../docs/screenshots/console-remote-config.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Password").fill("opamp-mock-password");
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(`${BASE}/`, { timeout: 60_000 });
await page.getByRole("link", { name: "Remote Config" }).click();
await page.getByTestId("compose-stepper").waitFor();

// Drive the stepper to a meaningful state: target, body, validate, propose.
await page.getByLabel(/Select shop-prod-collector/).check();
await page
  .getByLabel("Config body")
  .fill(
    "receivers:\n  otlp:\n    protocols:\n      grpc:\nexporters:\n  debug:\nservice:\n  pipelines:\n    logs:\n      receivers: [otlp]\n      exporters: [debug]\n",
  );
await page.getByRole("button", { name: "Validate" }).click();
await page.getByTestId("verdict-panel").waitFor();
await page.getByRole("button", { name: "Propose revision" }).click();
await page.getByTestId("proposal-status").waitFor();
await page.waitForTimeout(300);
await page.screenshot({ path: OUT, fullPage: true });
console.log(`saved ${OUT}`);
await browser.close();
