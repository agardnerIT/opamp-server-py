/**
 * Static fixtures for mock mode. Shaped exactly like the server payloads
 * (`AgentState.to_dict()` etc.) so the same code paths run against both.
 *
 * The fleet carries two agent types on purpose: stage 5's type-aware views
 * are developed and tested offline against these. The `agentTypes` entries
 * mirror `AgentProfile.describe()` (trimmed `identity`, full `panels` — the
 * detail page renders from those, never from hard-coded collector fields).
 */

import type { AgentEntry, AgentDetail, AgentTypeSummary } from "./endpoints";
import type { GroupsResponse } from "./groups";

const CONNECTED_AT = "2026-12-01T09:15:00Z";
const HEARTBEAT = "2026-12-01T09:44:12Z";

const OTCOL_ID = "6b1f3a4c-9d2e-4f70-8a1b-2c3d4e5f6071";
const GENERIC_ID = "0f2e8d7c-6b5a-4938-8271-6a5b4c3d2e10";

const agentTypes: AgentTypeSummary[] = [
  {
    name: "otelcol",
    display_name: "OpenTelemetry Collector",
    icon: "🧩",
    priority: 100,
    summary:
      "Collector YAML: components are read from AvailableComponents, structural validation applies, and the heavy/outdated reports and otelcol_* telemetry views are meaningful.",
    guidance:
      "**Remote config** is applied by the OpAMP Supervisor (`cmd/opampsupervisor`), not " +
      "by `extension/opampextension`: a bare collector never advertises `AcceptsRemoteConfig`.",
    validates_structure: true,
    content_types: ["", "application/x-yaml", "application/yaml", "text/plain", "text/x-yaml", "text/yaml"],
    default_filename: "otelcol-config.yaml",
    reports: ["fleet", "agent", "heavy", "outdated"],
    telemetry: {
      title: "Collector Telemetry",
      caption:
        "Real-time metrics from the collector's internal telemetry. These show how data flows through the collector.",
      raw_caption: "All available internal metrics from the collector:",
      metric_prefix: "otelcol_",
    },
    detectable: true,
    has_components: true,
    has_onboarding_snippet: true,
    panels: [
      {
        id: "data-flow-inputs",
        widget: "metrics",
        title: "Data Flow (Inputs)",
        caption: "Data being received by the collector. Sudden drops may indicate connectivity issues.",
        metrics: [
          { label: "Logs In", metric: "otelcol_receiver_accepted_log_records", format: "number" },
          { label: "Traces In", metric: "otelcol_receiver_accepted_spans", format: "number" },
          { label: "Metrics In", metric: "otelcol_receiver_accepted_metric_points", format: "number" },
          { label: "Refused", metric: "otelcol_receiver_refused_log_records", format: "number" },
        ],
      },
      {
        id: "data-flow-inputs-failed",
        widget: "alert",
        metric: "otelcol_receiver_failed_log_records",
        when: "above_zero",
        severity: "error",
        message: "⚠️ Failed: {value}",
      },
      {
        id: "data-flow-outputs",
        widget: "metrics",
        title: "Data Flow (Outputs)",
        caption:
          "Data being sent to downstream systems. If this drops but inputs are normal, check exporter connectivity.",
        metrics: [
          { label: "Logs Out", metric: "otelcol_exporter_sent_log_records", format: "number" },
          { label: "Traces Out", metric: "otelcol_exporter_sent_spans", format: "number" },
          { label: "Metrics Out", metric: "otelcol_exporter_sent_metric_points", format: "number" },
        ],
      },
      {
        id: "collector-health",
        widget: "metrics",
        title: "Collector Health",
        caption: "Resource usage of the collector process itself.",
        metrics: [
          { label: "CPU (sec)", metric: "otelcol_process_cpu_seconds", format: "decimal" },
          { label: "Memory (MB)", metric: "otelcol_process_memory_rss", format: "mebibytes" },
          { label: "Heap (MB)", metric: "otelcol_process_runtime_heap_alloc_bytes", format: "mebibytes" },
          { label: "Uptime (sec)", metric: "otelcol_process_uptime", format: "number" },
        ],
      },
    ],
    bootstrap_default: true,
  },
  {
    name: "generic",
    display_name: "Generic agent",
    icon: "📦",
    priority: 0,
    summary:
      "An agent whose configuration format this server does not interpret. Its config is stored, relayed and shown as opaque content, and the collector-specific reports and validation do not describe it.",
    guidance:
      "This server cannot validate, interpret or report on this agent's configuration: it stores " +
      "the body, relays it and shows it as opaque text.",
    validates_structure: false,
    content_types: [],
    default_filename: "config",
    reports: [],
    telemetry: null,
    detectable: false,
    has_components: false,
    has_onboarding_snippet: false,
    panels: [],
    bootstrap_default: false,
  },
];

const credential = { enrolled: true, active: 1, last_seen_at: HEARTBEAT, revoked_at: null };

const collectorDescription = {
  identifyingAttributes: [
    { key: "service.name", value: { stringValue: "io.opentelemetry.collector" } },
  ],
  nonIdentifyingAttributes: [
    { key: "service.version", value: { stringValue: "0.149.0" } },
    { key: "deployment.environment", value: { stringValue: "prod" } },
    { key: "host.arch", value: { stringValue: "amd64" } },
  ],
};

const genericDescription = {
  identifyingAttributes: [
    { key: "service.name", value: { stringValue: "acme-forwarder" } },
  ],
  nonIdentifyingAttributes: [
    { key: "deployment.environment", value: { stringValue: "lab" } },
  ],
};

const collectorListEntry: AgentEntry = {
  id: OTCOL_ID,
  name: "shop-prod-collector",
  healthy: true,
  health_status: "ok",
  health_error: null,
  connected_at: CONNECTED_AT,
  last_heartbeat: HEARTBEAT,
  capabilities: 8,
  capability_tags: [{ label: "Reports Status", icon: "📊" }],
  remote_config_status: "APPLIED",
  config_error: null,
  remote_config_error: null,
  agent_type: "otelcol",
  agent_type_source: "sniffed",
  agent_type_pinned: null,
  agent_type_ambiguous: null,
  agent_type_warning: null,
  credential,
};

const genericListEntry: AgentEntry = {
  id: GENERIC_ID,
  name: "lab-generic-forwarder",
  healthy: false,
  health_status: "config_conflict",
  health_error: "exporter endpoint unreachable",
  connected_at: CONNECTED_AT,
  last_heartbeat: HEARTBEAT,
  capabilities: 2,
  capability_tags: [{ label: "Reports Health", icon: "❤️" }],
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
  credential,
};

const collectorDetail: AgentDetail = {
  ...collectorListEntry,
  report_interval_seconds: 5.2,
  description: collectorDescription,
  effective_config: {
    configMap: {
      configMap: {
        "": {
          body: "extensions:\n  opamp:\n    server:\n      ws:\n        endpoint: ws://127.0.0.1:4320/v1/opamp\n          headers:\n            Authorization: \"Bearer <redacted>\"\n\nreceivers:\n  otlp:\n    protocols:\n      grpc:\n        endpoint: 127.0.0.1:4317\n\nprocessors:\n  batch:\n\nexporters:\n  debug:\n    verbosity: normal\n\nservice:\n  extensions: [opamp]\n  pipelines:\n    traces:\n      receivers: [otlp]\n      processors: [batch]\n      exporters: [debug]\n",
          contentType: "text/yaml",
        },
      },
    },
  },
  effective_config_redacted: true,
  redacted_paths: ['configMap.configMap."".headers.Authorization'],
  config_error: null,
  remote_config_error: null,
  pending_command: null,
  command_delivered_at: null,
  custom_capabilities: [],
  pending_custom_message: null,
  custom_message_delivered_at: null,
  uid_reissued_from: null,
  uid_reissue_reason: null,
  components_request_ignored: 0,
  components: {
    receivers: [
      { id: "otlp", used: true, version: "0.149.0" },
      { id: "prometheus", used: false, version: "0.149.0" },
    ],
    processors: [{ id: "batch", used: true, version: "0.149.0" }],
    exporters: [
      { id: "debug", used: true, version: "0.149.0" },
      { id: "otlphttp", used: false, version: "0.149.0" },
    ],
    extensions: [{ id: "opamp", used: true, version: "0.149.0" }],
  },
  components_status: "reported",
  compliance: {
    compliant: true,
    violations: [],
    policy_results: [{ name: "require-batch-processor", status: "pass", violations: [] }],
  },
  metrics: {
    metrics: {
      otelcol_receiver_accepted_log_records: 15234,
      otelcol_receiver_accepted_spans: 8721,
      otelcol_receiver_accepted_metric_points: 40211,
      otelcol_receiver_refused_log_records: 0,
      otelcol_receiver_failed_log_records: 0,
      otelcol_exporter_sent_log_records: 15234,
      otelcol_exporter_sent_spans: 8721,
      otelcol_exporter_sent_metric_points: 40210,
      otelcol_process_cpu_seconds: 12.4,
      otelcol_process_memory_rss: 78643200,
      otelcol_process_runtime_heap_alloc_bytes: 25165824,
      otelcol_process_uptime: 3125,
    },
    updated_at: HEARTBEAT,
  },
  stale_timeout_seconds: 120,
};

const genericDetail: AgentDetail = {
  ...genericListEntry,
  report_interval_seconds: 30,
  description: genericDescription,
  // An opaque JSON body: this profile's config is stored and shown as-is.
  effective_config: {
    configMap: {
      configMap: {
        "": {
          body: '{"listen": "0.0.0.0:9999", "upstream": "collector:4317"}',
          contentType: "application/json",
        },
      },
    },
  },
  effective_config_redacted: false,
  redacted_paths: [],
  config_error: null,
  remote_config_error: "apply failed: unknown receiver type",
  pending_command: null,
  command_delivered_at: null,
  custom_capabilities: [],
  pending_custom_message: null,
  custom_message_delivered_at: null,
  uid_reissued_from: null,
  uid_reissue_reason: null,
  components_request_ignored: 0,
  components: null,
  components_status: "none",
  compliance: { compliant: null, violations: [], opa_disabled: true },
  // A product fact, not a gap: this profile reports no telemetry to the server.
  metrics: {},
  stale_timeout_seconds: 300,
};

const agentDetails: Record<string, AgentDetail> = {
  [OTCOL_ID]: collectorDetail,
  [GENERIC_ID]: genericDetail,
};

/* ------------------------------------------------------------------ */
/* Config push (stage 6). Bodies live only in handlers that receive     */
/* them; these payloads are metadata, as the server's D8 rule keeps them.*/
/* ------------------------------------------------------------------ */

/** `GET /config/posture` — everything met, push allowed, not insecure. */
const configPosture = {
  allowed: true,
  insecure: false,
  unmet: [],
  config_push: { enabled: true, insecure: false, agent_auth: true, audit: true, tls: false, config_at_rest: "encrypted" },
  requirements: {
    agent_auth: { met: true, detail: "fleet token configured" },
    audit: { met: true, detail: "audit log enabled" },
    tls: { met: true, detail: "not required on loopback" },
    config_push_enabled: { met: true, detail: "OPAMP_CONFIG_PUSH_ENABLED=true" },
  },
};

/** `GET /groups` (stage 7 full shape): one SQL group and one legacy definition,
 * carrying the pin/warning fields the Groups page renders. */
const groups: GroupsResponse = {
  groups: [
    {
      group_id: "grp-mock-1",
      name: "all-collectors",
      selector_kind: "query",
      query: "SELECT * FROM agents WHERE agent_type = 'otelcol'",
      definition: null,
      include: [],
      exclude: [],
      description: "Every collector in the fleet",
      owner: "admin",
      source: "api",
      created_by: "admin",
      created_at: CONNECTED_AT,
      updated_by: "admin",
      updated_at: HEARTBEAT,
      member_count: 1,
      members: [OTCOL_ID],
      sources: { query: [OTCOL_ID], included: [], excluded: [] },
      unknown_pins: [],
      health_defined: [],
      membership_changes: 0,
    },
    {
      group_id: "grp-mock-2",
      name: "prod-agents",
      selector_kind: "definition",
      query: null,
      definition: { environment: "prod" },
      include: ["deadbeef0000"],
      exclude: [],
      description: "Prod filter plus a hand-pinned agent",
      owner: "platform-oncall",
      source: "api",
      created_by: "admin",
      created_at: CONNECTED_AT,
      updated_by: null,
      updated_at: null,
      member_count: 1,
      members: [OTCOL_ID],
      sources: { definition: [OTCOL_ID], included: [], excluded: [] },
      unknown_pins: ["deadbeef0000"],
      health_defined: [],
      membership_changes: 2,
    },
  ],
  count: 2,
};

/** Report markdown fixtures (stage 7) — shaped like `server/reports.py` output
 * for this fleet: the collector has 5 components (2 unused, 40% ≤ 50%), the
 * generic agent has no component concept (so it is "not applicable"). */
const reports = {
  fleet: {
    report_markdown:
      "# Agent Report\n\n" +
      "Generated: 2026-12-01 09:45:00\n\n" +
      "Total Agents: 2\n\n" +
      "## Component Versions\n" +
      "- **0.149.0**: 5 components\n\n" +
      "## Outdated Collectors\n" +
      "All collectors are up to date.\n\n" +
      "## Heavy Collectors (Unused Components)\n" +
      "- **6b1f3a4c-9d2e-4f**: 2/5 components unused (40%)\n\n" +
      "## Detailed Agent List\n" +
      "### 6b1f3a4c-9d2e-4f...\n\n" +
      "- Healthy: True\n" +
      "- Receivers: 2 total, 1 in use\n" +
      "- Processors: 1 total, 1 in use\n" +
      "- Exporters: 2 total, 1 in use\n" +
      "- Extensions: 1 total, 1 in use\n\n" +
      "### 0f2e8d7c-6b5a-49...\n\n" +
      "- Healthy: False\n\n",
    agent_count: 2,
  },
  heavy: {
    report_markdown:
      "# Heavy Collectors Report\n\n" +
      "Generated: 2026-12-01 09:45:00\n" +
      "Showing collectors with >50% unused components\n\n" +
      "Found 0 heavy collector(s)\n\n" +
      "Not applicable (report does not cover this profile): generic × 1\n",
    heavy_count: 0,
    threshold: 0.5,
    applicable_count: 1,
    not_applicable: { generic: 1 },
  },
  outdated: {
    report_markdown:
      "# Outdated Collectors Report\n\n" +
      "Generated: 2026-12-01 09:45:00\n" +
      "Latest version: v0.149.0\n\n" +
      "Found 0 outdated collector(s)\n\n" +
      "Not applicable (report does not cover this profile): generic × 1\n",
    collectors_count: 0,
    components_count: 0,
    version: "0.149.0",
    applicable_count: 1,
    not_applicable: { generic: 1 },
  },
};

/** Pre-existing revision history: an active one and a poisoned one. */
const revisions = [
  {
    revision_id: "rev-mock-a1",
    agent_id: OTCOL_ID,
    proposal_id: "prop-mock-old",
    status: "active",
    config_hash: "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    author: "admin",
    created_at: CONNECTED_AT,
    approved_by: "admin",
    approved_at: HEARTBEAT,
    note: "initial pipeline",
    parent_revision_id: null,
    withdrawn: false,
    failed_at: null,
    failure_detail: null,
    verdict: { valid: true, structure_checked: true, profile: "otelcol" },
    group_id: null,
    group_name: null,
  },
  {
    revision_id: "rev-mock-p1",
    agent_id: GENERIC_ID,
    proposal_id: "prop-mock-old",
    status: "poisoned",
    config_hash: "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
    author: "admin",
    created_at: CONNECTED_AT,
    approved_by: "admin",
    approved_at: HEARTBEAT,
    note: "osquery object test",
    parent_revision_id: null,
    withdrawn: false,
    failed_at: HEARTBEAT,
    failure_detail: "agent reported FAILED: expected a single osquery.json object",
    verdict: { valid: true, structure_checked: false, profile: "generic" },
    group_id: null,
    group_name: null,
  },
];

/** A completed rollout, so the Rollouts tab has history on first paint. */
const rollouts = [
  {
    rollout_id: "roll-mock-old",
    proposal_id: "prop-mock-older",
    kind: "rollout",
    status: "completed",
    created_at: CONNECTED_AT,
    batch: { current: 1, count: 1 },
    agents: 2,
    paused_reason: null,
    states: { applied: 2 },
  },
];

/* ------------------------------------------------------------------ */
/* Admin pages (stage 8): alerts, agent tokens, sessions, compliance,  */
/* updates. Alert webhook fields carry the server's redaction sentinel — */
/* the UI round-trips it verbatim and the server keeps the stored secret. */
/* ------------------------------------------------------------------ */

const alerts = {
  types: ["webhook"],
  events: ["new_agent", "agent_disconnected", "compliance_violation", "config_drift"],
  config: {
    events: {
      new_agent: {
        enabled: true,
        webhook_url: "https://hooks.example.com/T000/B000/***REDACTED***",
        headers: "***REDACTED***",
        body_template: JSON.stringify(
          {
            specversion: "1.0",
            type: "io.opentelemetry.opamp.agent.{event_type}",
            source: "opamp-server",
            id: "{id}",
            time: "{time}",
            datacontenttype: "application/json",
            data: { message: "{message}" },
          },
          null,
          2,
        ),
      },
      agent_disconnected: { enabled: false, webhook_url: "", headers: "", body_template: "" },
      compliance_violation: { enabled: false, webhook_url: "", headers: "", body_template: "" },
      config_drift: { enabled: false, webhook_url: "", headers: "", body_template: "" },
    },
  },
};

const agentTokens = {
  tokens: [
    {
      token_id: "tok_8f21c0",
      label: "prod fleet",
      agent_id: null,
      agent_type: "otelcol",
      created_at: "2026-11-20T09:00:00Z",
      expires_at: null as string | null,
      max_agents: 15,
      agents_used: 1,
      last_seen_at: "2026-12-01T10:40:00Z",
      revoked_at: null as string | null,
      expired: false,
      active: true,
    },
    {
      token_id: "tok_44aa19",
      label: "old lab token",
      agent_id: null,
      agent_type: null,
      created_at: "2026-10-02T09:00:00Z",
      expires_at: "2026-11-01T09:00:00Z",
      max_agents: null,
      agents_used: 0,
      last_seen_at: null,
      revoked_at: null,
      expired: true,
      active: false,
    },
  ],
  count: 2,
};

const adminSessions = {
  sessions: [
    {
      session_id: "9f2ab48c7711",
      principal_id: "legacy",
      principal: "admin (shared password)",
      created_at: "2026-12-01T08:00:00Z",
      last_seen_at: "2026-12-01T10:44:00Z",
      expires_at: "2026-12-01T11:00:00Z",
      client_ip: "127.0.0.1",
      user_agent: "Mozilla/5.0 (Macintosh)",
      expired: false,
    },
    {
      session_id: "5d0e91b3aa22",
      principal_id: "alice",
      principal: "alice",
      created_at: "2026-12-01T07:15:00Z",
      last_seen_at: "2026-12-01T09:30:00Z",
      expires_at: "2026-12-01T10:15:00Z",
      client_ip: "10.0.0.7",
      user_agent: "Mozilla/5.0 (X11; Linux)",
      expired: true,
    },
  ],
};

const policyValidation = {
  opa_enabled: true,
  policies: [
    {
      filename: "require-batch-processor.rego",
      valid: true,
      errors: [] as string[],
      warnings: [] as string[],
      name: "require-batch-processor",
      description: "Every collector must use the batch processor",
    },
    {
      filename: "broken-policy.rego",
      valid: false,
      errors: ["Missing package declaration: package opamp.agent.compliance.<name>"],
      warnings: [] as string[],
      name: null as string | null,
      description: null as string | null,
    },
  ],
};

const updates = {
  current_version: "0.9.0",
  latest_version: "0.9.1",
  latest_tag: "v0.9.1",
  update_available: true,
  checked_at: "2026-12-01T10:00:00Z",
  next_check_at: "2026-12-01T11:00:00Z",
  enabled: true,
  deployment_enabled: true,
  admin_override: null as boolean | null,
  source: "github",
  error: null as string | null,
  releases_url: "https://github.com/opamp-server-py/releases",
};

export const fixtures = {
  alerts,
  agentTokens,
  adminSessions,
  policyValidation,
  updates,
  agents: {
    agents: [collectorListEntry, genericListEntry],
    count: 2,
    filters: {},
  },
  agentTypes: { types: agentTypes, count: agentTypes.length },
  agentDetails,
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
        name: "name",
        sql_name: "name",
        kind: "core",
        description: "The agent's declared agent.name (NULL when it declared none)",
        values: ["shop-prod-collector", "lab-generic-forwarder"],
        agent_count: 2,
      },
      {
        name: "healthy",
        sql_name: "healthy",
        kind: "core",
        description: "1 healthy, 0 unhealthy, NULL when the agent never reported health",
        values: [1, 0],
        agent_count: 2,
      },
      {
        name: "health_status",
        sql_name: "health_status",
        kind: "core",
        description: "ComponentHealth.status, e.g. ok or config_conflict",
        values: ["ok", "config_conflict"],
        agent_count: 2,
      },
      {
        name: "remote_config_status",
        sql_name: "remote_config_status",
        kind: "core",
        description: "Remote config status, e.g. UNSET, APPLIED, FAILED",
        values: ["APPLIED", "FAILED"],
        agent_count: 2,
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
        name: "service.name",
        sql_name: '"service.name"',
        kind: "attribute",
        description: "service.name description attribute",
        values: ["io.opentelemetry.collector", "acme-forwarder"],
        agent_count: 2,
      },
      {
        name: "deployment.environment",
        sql_name: '"deployment.environment"',
        kind: "attribute",
        description: "deployment.environment description attribute",
        values: ["prod", "lab"],
        agent_count: 2,
      },
      {
        name: "host.arch",
        sql_name: '"host.arch"',
        kind: "attribute",
        description: "host.arch description attribute",
        values: ["amd64"],
        agent_count: 1,
      },
    ],
    count: 9,
    examples: [
      "SELECT * FROM agents",
      "SELECT * FROM agents WHERE healthy = 1",
      "SELECT * FROM agents WHERE remote_config_status = 'APPLIED'",
      "SELECT * FROM agents WHERE type = 'otelcol'",
    ],
    functions: {
      attr: "attr('key') is shorthand for the attribute column named key",
    },
    operators: ["=", "!=", "LIKE", "IN", "IS NULL"],
  },
  authStatus: {
    password_required: true,
    // Principals mode: per-person identities, so the login form shows the
    // name field (stage 9 role-denial cases sign in as named principals).
    auth_mode: "principals",
    roles: ["admin", "approver", "operator", "viewer"],
    sessions: true,
    principals: 4,
    legacy_password: false,
  },
  /** Password `mock.ts` accepts — also the Playwright credential. */
  validPassword: "opamp-mock-password",
  /**
   * Mock principals (stage 9 role-denial cases): `POST /auth/session` maps a
   * login name onto a role the way the server maps a principal record onto
   * one — name `viewer`/`operator`/`approver` signs in with that role; any
   * other name (or none) is the legacy shared-password admin. Same password
   * for all, so a test switches role by switching only the name.
   */
  mockPrincipals: {
    viewer: "viewer",
    operator: "operator",
    approver: "approver",
  } as Record<string, string>,
  /** Role for the `admin` principal name. */
  defaultRole: "admin",
  /** `POST /auth/session` 201 body — token included, exactly like the server. */
  session: {
    token: "mock-session-token-not-a-real-credential",
    token_type: "bearer",
    expires_at: "2026-12-01T10:44:12Z",
    principal: { name: "admin", role: "admin" },
    ttl_seconds: 3600,
    idle_timeout_seconds: 900,
  },
  configPosture,
  groups,
  reports,
  revisions,
  rollouts,
  /** `GET /auth/me` for a live session. */
  me: {
    authenticated: true,
    name: "admin",
    role: "admin",
    method: "session",
    roles: ["admin", "approver", "operator", "viewer"],
    session_expires_at: "2026-12-01T10:44:12Z",
    session_idle_timeout_seconds: 900,
  },
};
