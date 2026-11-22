import { defineConfig, devices } from "@playwright/test";

/**
 * E2E runs against the Vite dev server in mock mode (`VITE_USE_MOCK_API=1`),
 * so the suite needs no live FastAPI server and no real cookie jar — the mock
 * transport's stateful auth handlers stand in for the httpOnly cookie.
 *
 * Stage 9 owns the full Playwright parity suite (against the live server too);
 * stage 3 lands the login failure/success round-trip required by its done-when.
 */
export default defineConfig({
  testDir: "./e2e",
  // The live smoke has its own config (playwright.live.config.ts) that boots a
  // real server; here it would run against the mock and fail on purpose.
  testIgnore: "**/live.smoke.spec.ts",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://localhost:5199",
  },
  webServer: {
    command: "npm run dev -- --port 5199 --strictPort",
    url: "http://localhost:5199",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      VITE_USE_MOCK_API: "1",
    },
  },
});
