/**
 * Static fixtures for mock mode. Shaped exactly like the server payloads
 * (`AgentState.to_dict()` etc.) so the same code paths run against both.
 */

import type { AgentEntry } from "./endpoints";

const CONNECTED_AT = "2026-12-01T09:15:00Z";
const HEARTBEAT = "2026-12-01T09:44:12Z";

export const fixtures = {
  agents: {
    agents: [
      {
        id: "6b1f3a4c-9d2e-4f70-8a1b-2c3d4e5f6071",
        name: "shop-prod-collector",
        healthy: true,
        health_status: "ok",
        health_error: null,
        connected_at: CONNECTED_AT,
        last_heartbeat: HEARTBEAT,
        capabilities: 8,
        capability_tags: ["accepts_remote_config"],
        remote_config_status: "APPLIED",
        config_error: null,
        remote_config_error: null,
        agent_type: "otelcol",
        agent_type_source: "sniffed",
        agent_type_pinned: null,
        agent_type_ambiguous: null,
        agent_type_warning: null,
        credential: { enrolled: true, active: 1, last_seen_at: HEARTBEAT, revoked_at: null },
      } satisfies Partial<AgentEntry> as AgentEntry,
      {
        id: "0f2e8d7c-6b5a-4938-8271-6a5b4c3d2e10",
        name: "lab-generic-forwarder",
        healthy: false,
        health_status: "config_conflict",
        health_error: "exporter endpoint unreachable",
        connected_at: CONNECTED_AT,
        last_heartbeat: HEARTBEAT,
        capabilities: 2,
        capability_tags: ["reports_health"],
        remote_config_status: "FAILED",
        config_error: null,
        remote_config_error: "apply failed: unknown receiver type",
        agent_type: "generic",
        agent_type_source: "pinned",
        agent_type_pinned: "generic",
        agent_type_ambiguous: null,
        agent_type_warning:
          "this agent resolves to the 'generic' profile, so the server does not " +
          "interpret its configuration: collector-specific checks, reports and " +
          "telemetry views do not describe it",
        credential: { enrolled: true, active: 1, last_seen_at: HEARTBEAT, revoked_at: null },
      } satisfies Partial<AgentEntry> as AgentEntry,
    ],
    count: 2,
    filters: {},
  },
  querySchema: {
    table: "agents",
    columns: [
      {
        name: "agent_id",
        sql_name: "agent_id",
        kind: "core",
        description: "Agent instance uid (same value as id)",
        values: [],
        agent_count: 0,
      },
      {
        name: "agent_type",
        sql_name: "agent_type",
        kind: "core",
        description: "Resolved profile name, e.g. otelcol or generic",
        values: ["otelcol", "generic"],
        agent_count: 2,
      },
      {
        name: "healthy",
        sql_name: "healthy",
        kind: "core",
        description: "'yes', 'no' or 'unknown' from reported health",
        values: ["yes", "no"],
        agent_count: 2,
      },
    ],
    examples: ["SELECT * FROM agents WHERE healthy = 'no'"],
  },
  authStatus: {
    password_required: true,
    auth_mode: "password",
    roles: ["admin", "approver", "operator", "viewer"],
    sessions: true,
    principals: 1,
    legacy_password: false,
  },
};
