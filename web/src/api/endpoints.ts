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
