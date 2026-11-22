import createClient from "openapi-fetch";
import type { paths } from "./schema";
import { mockFetch, mockApiEnabled } from "./mock";

/**
 * The only HTTP entry point for the app. All requests go through this typed
 * client bound to `/api` (same origin in dev via the Vite proxy, same origin
 * behind the nginx proxy in production). Components never call `fetch`.
 *
 * The base defaults to the relative `/api`; `VITE_API_BASE_URL` overrides it
 * for node-context tests, where a relative Request URL is invalid (vitest
 * pins `http://mock.local/api`).
 */
export const api = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL || "/api",
  // Dev-without-server and Playwright runs can stub the network here instead
  // of reaching a live backend.
  fetch: mockApiEnabled() ? mockFetch : undefined,
});
