/**
 * Admin-page wrappers (new-ui stage 8): alerts, agent tokens, admin sessions,
 * compliance, update checks. Same pattern as `reports.ts` — the generated
 * schema types these payloads as `unknown` (FastAPI `dict` responses), so the
 * narrow interfaces below mirror the server handlers in `server/main.py`.
 *
 * Redaction (alerts): `webhook_url`/`headers` come back masked with the
 * sentinel `***REDACTED***`. Sending the sentinel back unchanged on
 * `PUT /alerts` keeps the stored value (server/alerts.py `merge_redacted`), so
 * the UI round-trips whatever GET returned — it must never "clean up" the
 * sentinel, or saving would wipe the webhook. This is the alerts analogue of
 * the config-body `<redacted>` rule; here the sentinel IS the API contract.
 */

import { api } from "./client";
import { unwrap } from "./errors";

/* ------------------------------------------------------------------ */
/* Alerts                                                              */
/* ------------------------------------------------------------------ */

export interface AlertEventConfig {
  enabled: boolean;
  webhook_url: string;
  headers: string;
  body_template: string;
}

export interface AlertsResponse {
  types: string[];
  events: string[];
  /** Redacted per event; missing fields fall back to the server defaults. */
  config: { events: Record<string, Partial<AlertEventConfig>> };
}

export async function getAlerts(): Promise<AlertsResponse> {
  return unwrap<AlertsResponse>(api.GET("/alerts"));
}

/** Merge-save the per-event config; sentinel values keep the stored secret. */
export async function updateAlerts(
  events: Record<string, AlertEventConfig>,
): Promise<AlertsResponse["config"]> {
  const payload = await unwrap<{ config: AlertsResponse["config"] }>(
    api.PUT("/alerts", { body: { events } }),
  );
  return payload.config;
}

/** Fire a test alert; `eventConfig` validates unsaved settings for one send. */
export async function testAlert(
  eventType: string,
  eventConfig: AlertEventConfig,
): Promise<{ success: boolean; error: string | null }> {
  return unwrap(
    api.POST("/alerts/test", { body: { event_type: eventType, event_config: eventConfig } }),
  );
}

/* ------------------------------------------------------------------ */
/* Agent tokens                                                        */
/* ------------------------------------------------------------------ */

/** One row of `GET /agent-tokens` — never a token, never a hash. */
export interface AgentTokenRow {
  token_id: string;
  label: string | null;
  /** Bound token only; fleet tokens are `null`. */
  agent_id: string | null;
  /** Profile the snippet was minted for (#144); `null` = unspecified. */
  agent_type: string | null;
  created_at: string | null;
  expires_at: string | null;
  max_agents: number | null;
  agents_used: number;
  last_seen_at: string | null;
  revoked_at: string | null;
  /** Computed server-side — do not re-derive. */
  expired: boolean;
  active: boolean;
}

export interface MintedToken {
  /** The plaintext credential — shown once, never stored in the UI. */
  token: string;
  token_id: string;
  label: string | null;
  agent_type: string | null;
  created_at: string | null;
  expires_at: string | null;
  max_agents: number | null;
  /** Where the snippet tells agents to connect (the server's PUBLIC_URL). */
  endpoint: string;
  collector_snippet: string;
  note: string;
}

export async function listAgentTokens(): Promise<{ tokens: AgentTokenRow[]; count: number }> {
  return unwrap(api.GET("/agent-tokens"));
}

export async function mintFleetToken(body: {
  /** Required by the schema (defaults to ""); empty means unlabelled. */
  label?: string;
  expires_in_days?: number | null;
  max_agents?: number | null;
  agent_type?: string | null;
}): Promise<MintedToken> {
  return unwrap(api.POST("/agent-tokens", { body: { label: "", ...body } }));
}

export async function revokeAgentToken(
  tokenId: string,
): Promise<{ token_id: string; revoked: boolean }> {
  return unwrap(
    api.DELETE("/agent-tokens/{token_id}", { params: { path: { token_id: tokenId } } }),
  );
}

/* ------------------------------------------------------------------ */
/* Admin sessions                                                      */
/* ------------------------------------------------------------------ */

/** One row of `GET /auth/sessions` (`session_view`) — never the token. */
export interface AdminSessionRow {
  /** 12-char hash prefix; pass it to `DELETE /auth/sessions/{id}`. */
  session_id: string;
  principal_id: string | null;
  principal: string;
  created_at: string | null;
  last_seen_at: string | null;
  expires_at: string | null;
  client_ip: string | null;
  user_agent: string | null;
  expired: boolean;
}

export async function listAdminSessions(): Promise<{ sessions: AdminSessionRow[] }> {
  return unwrap(api.GET("/auth/sessions"));
}

export async function revokeAdminSession(
  sessionId: string,
): Promise<{ session_id: string; revoked: boolean }> {
  return unwrap(
    api.DELETE("/auth/sessions/{session_id}", {
      params: { path: { session_id: sessionId } },
    }),
  );
}

/* ------------------------------------------------------------------ */
/* Compliance                                                          */
/* ------------------------------------------------------------------ */

/** One entry of `GET /compliance/validate` (`validate_policy_file`). */
export interface PolicyValidation {
  filename: string;
  valid: boolean;
  errors: string[];
  warnings: string[];
  name: string | null;
  description: string | null;
}

export async function getPolicyValidation(): Promise<{
  opa_enabled: boolean;
  policies: PolicyValidation[];
}> {
  return unwrap(api.GET("/compliance/validate"));
}

export async function reloadPolicies(): Promise<{ success: boolean; error?: string }> {
  return unwrap(api.POST("/compliance/reload"));
}

/** `POST /compliance/check/{id}` — writes the verdict onto the agent. */
export interface ComplianceCheckResult {
  agent_id?: string;
  compliant?: boolean;
  violations?: string[];
  error?: string;
  opa_disabled?: boolean;
}

export async function checkAgentCompliance(
  agentId: string,
): Promise<ComplianceCheckResult> {
  return unwrap(
    api.POST("/compliance/check/{agent_id}", { params: { path: { agent_id: agentId } } }),
  );
}

/* ------------------------------------------------------------------ */
/* Update checks                                                       */
/* ------------------------------------------------------------------ */

/** `GET /updates` — cached state, never performs network I/O. */
export interface UpdatesState {
  current_version: string;
  latest_version: string | null;
  latest_tag: string | null;
  update_available: boolean;
  checked_at: string | null;
  next_check_at: string | null;
  /** The effective state (deployment flag wins over the admin toggle). */
  enabled: boolean;
  deployment_enabled: boolean;
  admin_override: boolean | null;
  source: string | null;
  error: string | null;
  releases_url: string | null;
}

export async function getUpdates(): Promise<UpdatesState> {
  return unwrap(api.GET("/updates"));
}

/** Persist the admin toggle; `null` clears the override (deployment default). */
export async function putUpdateSettings(
  enabled: boolean | null,
): Promise<UpdatesState> {
  return unwrap(api.PUT("/updates/settings", { body: { enabled } }));
}

export async function runUpdateCheck(): Promise<UpdatesState> {
  return unwrap(api.POST("/updates/check"));
}
