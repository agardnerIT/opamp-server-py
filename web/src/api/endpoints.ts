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
  /** `null` when the agent never reported health. */
  healthy: boolean | null;
  /** Present in `to_dict()`; attribute-column cells read from it. */
  description?: unknown;
  health_status: string | null;
  health_error: string | null;
  connected_at: string;
  last_heartbeat: string;
  capabilities: number;
  /** `CAPABILITY_TAGS` — `{label, icon}` pairs, not bare strings (server/state.py). */
  capability_tags: { label: string; icon: string }[];
  remote_config_status: string | null;
  config_error: unknown;
  remote_config_error: string | null;
  /** OPA verdict, present when compliance has been evaluated. */
  compliance?: {
    compliant?: boolean | null;
    opa_disabled?: boolean;
    violations?: string[];
  } | null;
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

/** An OpAMP description attribute value: a protobuf oneOf, first key wins. */
export function attrValue(description: unknown, key: string): string | null {
  if (!description || typeof description !== "object") return null;
  const doc = description as {
    identifyingAttributes?: { key: string; value?: Record<string, unknown> }[];
    nonIdentifyingAttributes?: { key: string; value?: Record<string, unknown> }[];
  };
  for (const attrs of [doc.identifyingAttributes, doc.nonIdentifyingAttributes]) {
    for (const attr of attrs ?? []) {
      if (attr.key === key && attr.value) {
        const first = Object.values(attr.value)[0];
        if (first !== undefined) return String(first);
      }
    }
  }
  return null;
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
/* Agent detail + agent types (new-ui stage 5).                        */
/* ------------------------------------------------------------------ */

/**
 * Full single-agent payload: `AgentState.to_dict()` plus the three keys
 * `GET /agent/{id}` adds (`metrics`, `credential`, `stale_timeout_seconds`).
 * Only the fields the UI renders are named; the rest are reachable via the
 * generated schema if a later stage needs them.
 */
export interface AgentDetail {
  id: string;
  name: string | null;
  /** `null` when the agent never reported health. */
  healthy: boolean | null;
  health_status: string | null;
  health_error: string | null;
  connected_at: string;
  last_heartbeat: string;
  report_interval_seconds: number | null;
  capabilities: number;
  capability_tags: { label: string; icon: string }[];
  description: unknown;
  /** Redacted at the API edge — a `<redacted>` sentinel means "keep stored". */
  effective_config: unknown;
  effective_config_redacted: boolean;
  redacted_paths: string[];
  config_error: unknown;
  remote_config_status: string | null;
  remote_config_error: string | null;
  pending_command: string | null;
  command_delivered_at: string | null;
  custom_capabilities: string[];
  pending_custom_message: { capability?: string; type?: string } | null;
  custom_message_delivered_at: string | null;
  uid_reissued_from: string | null;
  uid_reissue_reason: string | null;
  components_request_ignored: number;
  /** Section name → list of `{id, used, version?, health?}` entries. */
  components: Record<string, { id: string; used?: boolean; version?: string }[]> | null;
  components_status: string | null;
  compliance: {
    compliant?: boolean | null;
    violations?: string[];
    opa_disabled?: boolean;
    policy_results?: { name: string; status: string; violations: string[] }[];
  } | null;
  /** Profile fields (#118). */
  agent_type: string;
  agent_type_source: "pinned" | "declared" | "sniffed" | "fallback";
  agent_type_pinned: string | null;
  agent_type_ambiguous: string[] | null;
  agent_type_warning: string | null;
  /** `GET /agent/{id}` additions. */
  metrics: { metrics?: Record<string, number>; updated_at?: string } | null;
  credential: {
    enrolled: boolean;
    active: number;
    last_seen_at: string | null;
    revoked_at: string | null;
  };
  stale_timeout_seconds: number;
}

export async function getAgent(agentId: string): Promise<AgentDetail> {
  const result = await unwrap<unknown>(api.GET("/agent/{agent_id}", { params: { path: { agent_id: agentId } } }));
  return result as AgentDetail;
}

/** One entry of `GET /agent/types` — `AgentProfile.describe()` (server/profiles/base.py). */
export interface AgentTypeSummary {
  name: string;
  display_name: string;
  icon: string;
  priority: number;
  summary: string;
  guidance: string;
  validates_structure: boolean;
  content_types: string[];
  default_filename: string;
  reports: string[];
  /** Profile telemetry block, present only when these agents report metrics. */
  telemetry: {
    title: string;
    caption: string;
    raw_caption: string;
    metric_prefix?: string;
  } | null;
  detectable: boolean;
  has_components: boolean;
  has_onboarding_snippet: boolean;
  /** Detail-page blocks, declared in `server/profiles/data/*.yaml` (#121). */
  panels: {
    id: string;
    widget: "metrics" | "alert" | string;
    title?: string;
    caption?: string;
    metrics?: { label: string; metric: string; format: string }[];
    metric?: string;
    when?: string;
    severity?: string;
    message?: string;
  }[];
  bootstrap_default: boolean;
}

export interface AgentTypesResponse {
  types: AgentTypeSummary[];
  count: number;
}

export async function getAgentTypes(): Promise<AgentTypesResponse> {
  return unwrap<AgentTypesResponse>(api.GET("/agent/types"));
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
 * from freshly entered credentials. NOTE: DELETE must come first — a second POST
 * would move the cookie to the NEW session, so the subsequent DELETE (which
 * follows the cookie) would revoke the wrong one. DELETE first (cookie still
 * points at the old session), then POST. If the POST fails the caller is simply
 * logged out; that is the honest outcome, not a silent extension.
 */
export async function extendSession(name: string, password: string): Promise<LoginResult> {
  await logout();
  return login(name, password);
}
