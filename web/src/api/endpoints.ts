/**
 * Thin, typed wrappers for the endpoints the UI reads. The generated schema
 * (`schema.d.ts`) stays the source of truth for paths/params; these functions
 * exist only where ergonomics demand it:
 * - several server payloads are `Dict[str, Any]` in FastAPI, so the generated
 *   response type is `unknown` — the narrow interfaces below mirror
 *   `AgentState.to_dict()` / `credential_summary()` and are validated only by
 *   the compiler, not at runtime.
 * - every wrapper throws the normalized `ApiError` (see errors.ts) instead of
 *   returning `{ data?, error? }` unions.
 */

import { api } from "./client";
import { unwrap } from "./errors";

/** Mirrors `AgentState.to_dict()` (server/state.py) — the `/agents` entry. */
export interface AgentEntry {
  id: string;
  /** The agent's own label; `null` is a fact, not a fallback. Display only. */
  name: string | null;
  healthy: boolean;
  health_status: string | null;
  health_error: string | null;
  connected_at: string;
  last_heartbeat: string;
  capabilities: number;
  capability_tags: string[];
  remote_config_status: string | null;
  config_error: unknown;
  remote_config_error: string | null;
  /** Profile fields (#118) added by `type_info()`. */
  agent_type: string;
  agent_type_source: "pinned" | "declared" | "sniffed" | "fallback";
  agent_type_pinned: string | null;
  agent_type_ambiguous: string[] | null;
  agent_type_warning: string | null;
  /** Mirrors `credential_summary()` — never a token, never a hash. */
  credential: {
    enrolled: boolean;
    active: number;
    last_seen_at: string | null;
    revoked_at: string | null;
  };
}

export interface AgentsResponse {
  agents: AgentEntry[];
  count: number;
  filters: Record<string, unknown>;
}

export interface ListAgentsParams {
  healthy?: "true" | "false" | "unknown";
  status?: string;
  remote_config_status?: string;
  q?: string;
  group?: string;
}

export async function listAgents(params: ListAgentsParams = {}): Promise<AgentsResponse> {
  const payload = await unwrap<AgentsResponse>(
    api.GET("/agents", { params: { query: { ...params } } }),
  );
  return payload;
}

/** One entry of `GET /agents/query/schema` (#132 autosuggestion payload). */
export interface QuerySchemaColumn {
  name: string;
  sql_name: string;
  kind: string;
  description: string;
  values: unknown[];
  agent_count: number;
}

export interface QuerySchemaResponse {
  table: string;
  columns: QuerySchemaColumn[];
  examples?: string[];
}

export async function getQuerySchema(): Promise<QuerySchemaResponse> {
  return unwrap<QuerySchemaResponse>(api.GET("/agents/query/schema"));
}

/* ------------------------------------------------------------------ */
/* Auth (cookie sessions — new-ui stage 3). The browser never holds    */
/* the session token: the server sets it as an httpOnly cookie and the */
/* token in the login response body is discarded here, never returned  */
/* to callers and never persisted (plan rule: no localStorage).        */
/* ------------------------------------------------------------------ */

/** `GET /auth/status` — how this server wants to be authenticated. */
export interface AuthStatus {
  password_required: boolean;
  auth_mode: "password" | "principals" | "disabled";
  roles: string[];
  sessions: boolean;
  principals: number;
  legacy_password: boolean;
}

export async function getAuthStatus(): Promise<AuthStatus> {
  return unwrap<AuthStatus>(api.GET("/auth/status"));
}

/** `GET /auth/me` — identity, role and session expiry for the session slot. */
export interface WhoAmI {
  authenticated: boolean;
  name: string;
  role: string;
  /** `session` (cookie/bearer), `password`, `legacy` or `anonymous` (read-only). */
  method: string;
  roles: string[];
  session_expires_at: string | null;
  session_idle_timeout_seconds: number;
}

export async function getMe(): Promise<WhoAmI> {
  return unwrap<WhoAmI>(api.GET("/auth/me"));
}

/** Login response minus the token — the one secret this UI must drop. */
export interface LoginResult {
  principal: { name: string; role: string };
  expires_at: string;
  ttl_seconds: number;
  idle_timeout_seconds: number;
}

/**
 * `POST /auth/session`. The 201 body carries the bearer token once; the cookie
 * (set by the same response) is the browser's transport, so the token is
 * stripped here and never exposed further. Bad credentials are 401, a locked
 * out identity is 429 (with Retry-After) — `errors.ts` classifies both.
 */
export async function login(name: string, password: string): Promise<LoginResult> {
  const result = await unwrap<Record<string, unknown>>(
    api.POST("/auth/session", { body: { name, password } }),
  );
  // Pick the non-secret fields explicitly — `token` stays inside this function.
  return {
    principal: result.principal as LoginResult["principal"],
    expires_at: result.expires_at as string,
    ttl_seconds: result.ttl_seconds as number,
    idle_timeout_seconds: result.idle_timeout_seconds as number,
  };
}

/** `DELETE /auth/session` — revokes the session AND clears the cookie. */
export async function logout(): Promise<{ logged_out: boolean }> {
  return unwrap<{ logged_out: boolean }>(api.DELETE("/auth/session"));
}

/**
 * Audited re-auth ("Extend"): revoke the current session, then mint a new one
 * from freshly entered credentials. NOTE: this is the reverse of the Streamlit
 * UI's POST-then-DELETE order — in the cookie flow a second POST would move the
 * cookie to the NEW session, so the subsequent DELETE (which follows the cookie)
 * would revoke the wrong one. DELETE first (cookie still points at the old
 * session), then POST. If the POST fails the caller is simply logged out.
 */
export async function extendSession(name: string, password: string): Promise<LoginResult> {
  await logout();
  return login(name, password);
}
