import createClient from "openapi-fetch";
import type { paths } from "./schema";
import { mockFetch, mockApiEnabled } from "./mock";

/**
 * The only HTTP entry point for the app. All requests go through this typed
 * client bound to `/api` (same origin in dev via the Vite proxy, same origin
 * behind the nginx proxy in production). Components never call `fetch`.
 */
export const api = createClient<paths>({
  baseUrl: "/api",
  // Dev-without-server and Playwright runs can stub the network here instead
  // of reaching a live backend.
  fetch: mockApiEnabled() ? mockFetch : undefined,
});
