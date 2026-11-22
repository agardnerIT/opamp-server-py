import { defineConfig } from "vitest/config";

/**
 * Unit tests always run against the mock transport: the API layer's own tests
 * (and any future component tests) must never reach the network or require a
 * live server. Set before any module init, so `client.ts` binds `mockFetch`.
 */
export default defineConfig({
  test: {
    // Unit tests live in src/ as *.test.ts. Scoped include so the default
    // `**/*.spec.*` pattern never picks up the Playwright specs in e2e/
    // (those run via `npm run e2e`).
    include: ["src/**/*.test.{ts,tsx}"],
    env: {
      VITE_USE_MOCK_API: "1",
      // openapi-fetch builds `new Request(baseUrl + path)` before the custom
      // fetch runs — relative URLs are invalid in node, so pin a base here.
      VITE_API_BASE_URL: "http://mock.local/api",
    },
  },
});
