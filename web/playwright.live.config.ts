import { defineConfig, devices } from "@playwright/test";

/**
 * Live-server smoke (new-ui stage 9): the same UI against a real FastAPI
 * server, no mock. `npm run e2e:live` boots a throwaway server (temp DATA_DIR,
 * throwaway ADMIN_PASSWORD, config push off → read-only) on port 4330 — never
 * the dev stack's server on 4320, whose fleet and credentials must not be
 * touched by a test — plus a Vite dev server proxying to it.
 *
 * Scope is deliberately small: what the mock cannot prove is that the real
 * cookie round-trip (Set-Cookie on login, sent on requests, cleared on
 * logout) and the real payloads land in the browser. Fleet-dependent views
 * (agents, groups, rollouts) belong to the mock suite.
 */

const API_PORT = 4330;
const API_URL = `http://127.0.0.1:${API_PORT}`;
const SMOKE_PASSWORD = "live-smoke-password";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/live.smoke.spec.ts",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://localhost:5199",
  },
  webServer: [
    {
      command:
        `rm -rf /tmp/opamp-live-smoke-data && mkdir -p /tmp/opamp-live-smoke-data && ` +
        `DATA_DIR=/tmp/opamp-live-smoke-data ` +
        `ADMIN_PASSWORD=${SMOKE_PASSWORD} ` +
        `OPAMP_CONFIG_PUSH_ENABLED=false ` +
        `.venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port ${API_PORT}`,
      url: `${API_URL}/health`,
      cwd: "..",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --port 5199 --strictPort",
      url: "http://localhost:5199",
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      env: {
        // Vite proxies /api here (vite.config.ts) — the throwaway server, not
        // the dev stack's.
        OPAMP_API_TARGET: API_URL,
      },
    },
  ],
});
