/**
 * Typed wrappers for the config-push flow (new-ui stage 6): posture, saved
 * groups (read-only — CRUD is stage 7), validation, the propose → approve →
 * rollout lifecycle and its history.
 *
 * Every narrow interface below mirrors the server payload it names; nothing
 * here invents states or fields (plan rule 3: the server is the authority).
 * Config bodies are only ever sent, never read back through this module —
 * the listing endpoints carry metadata only (D8), so there is nothing to
 * un-redact and a `<redacted>` sentinel can never flow back to the server
 * from here.
 */

import { api } from "./client";
import { unwrap } from "./errors";

/* ------------------------------------------------------------------ */
/* Posture                                                             */
/* ------------------------------------------------------------------ */

/** `GET /config/posture` — the fields the page banner needs. */
export interface ConfigPosture {
  allowed: boolean;
  insecure: boolean;
  unmet: string[];
  requirements: Record<string, { met: boolean; detail?: string } | null>;
  config_push?: { enabled?: boolean; insecure?: boolean } | null;
}

export async function getConfigPosture(): Promise<ConfigPosture> {
  return unwrap<ConfigPosture>(api.GET("/config/posture"));
}

/* ------------------------------------------------------------------ */
/* Saved groups (read-only; the Groups page is stage 7)                */
/* ------------------------------------------------------------------ */

/** One entry of `GET /groups` — `groups.view()` minus the history blocks. */
export interface SavedGroup {
  group_id: string;
  name: string;
  selector_kind: string;
  query: string | null;
  definition: unknown;
  include: string[];
  exclude: string[];
  member_count: number;
  members: string[];
  /** Which health keys the definition uses — pushing to these is guarded. */
  health_defined: string[];
  sources: Record<string, string[]> | null;
  unknown_pins: string[] | null;
}

export interface GroupsResponse {
  groups: SavedGroup[];
  count: number;
}

export async function listGroups(): Promise<GroupsResponse> {
  return unwrap<GroupsResponse>(api.GET("/groups"));
}

/* ------------------------------------------------------------------ */
/* Validation (no storage, no side effects)                            */
/* ------------------------------------------------------------------ */

/** One validation finding (`config_validation.Finding.to_dict()`). */
export interface ValidationFinding {
  severity: string;
  path: string;
  message: string;
}

/** The #100 pipeline verdict, as `POST /config/validate` returns it. */
export interface Verdict {
  valid: boolean;
  errors: string[];
  details: ValidationFinding[];
  warnings: string[];
  policy: { status?: string; violations?: string[] } | null;
  config_hash: string | null;
  size_bytes: number | null;
  objects: number | null;
  profile: string | null;
  profile_source: string | null;
  structure_checked: boolean | null;
}

/**
 * `POST /config/validate` — always 200; `valid: false` with `errors` is the
 * answer, so callers render the verdict instead of catching.
 */
export async function validateConfig(
  configMap: Record<string, unknown>,
  agentId?: string | null,
): Promise<Verdict> {
  return unwrap<Verdict>(
    api.POST("/config/validate", {
      body: { config_map: configMap, agent_id: agentId ?? null, allow_endpoint_change: false },
    }),
  );
}

/* ------------------------------------------------------------------ */
/* Proposals and revisions                                             */
/* ------------------------------------------------------------------ */

/** The scope of a proposal — exactly one of the four keys is set (server `ConfigScope`). */
export interface ConfigScope {
  agent_id?: string | null;
  agents?: string[] | null;
  filter?: Record<string, unknown> | null;
  group?: string | null;
  include_future?: boolean;
  /** Only meaningful with a health-defined group — the UI never sets it. */
  use_stored_resolution?: boolean;
}

/** Response of `POST /config/revisions` — `config_control.propose()`. */
export interface ProposeResult {
  proposal_id: string;
  status: string;
  config_hash: string;
  agents: number;
  scope: Record<string, unknown>;
  revisions: { agent_id: string; revision_id: string }[];
}

export interface ProposeConfigInput {
  configMap: Record<string, unknown>;
  scope: ConfigScope;
  note?: string | null;
  confirmScopeChange?: boolean;
  allowEndpointChange?: boolean;
  overrideReason?: string | null;
}

export async function proposeConfig(input: ProposeConfigInput): Promise<ProposeResult> {
  return unwrap<ProposeResult>(
    api.POST("/config/revisions", {
      body: {
        config_map: input.configMap,
        // The generated schema types the defaulted booleans as required; the
        // UI never sends them otherwise.
        scope: { ...input.scope, include_future: input.scope.include_future ?? false, use_stored_resolution: false },
        note: input.note ?? null,
        confirm_scope_change: input.confirmScopeChange ?? false,
        allow_endpoint_change: input.allowEndpointChange ?? false,
        override_reason: input.overrideReason ?? null,
      },
    }),
  );
}

/** One revision's metadata — `config_control.revision_view()` (never a body, D8). */
export interface ConfigRevision {
  revision_id: string;
  agent_id: string;
  proposal_id: string | null;
  status: string | null;
  config_hash: string | null;
  author: string | null;
  created_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  note: string | null;
  parent_revision_id: string | null;
  withdrawn: boolean;
  failed_at: string | null;
  failure_detail: string | null;
  verdict: Verdict | null;
  /** Group columns of `_group_view()` — present when the scope was a group. */
  group_id?: string | null;
  group_name?: string | null;
}

export interface RevisionsResponse {
  revisions: ConfigRevision[];
  count: number;
  statuses: string[];
}

export async function listConfigRevisions(
  params: { agent?: string; status?: string; limit?: number } = {},
): Promise<RevisionsResponse> {
  return unwrap<RevisionsResponse>(
    api.GET("/config/revisions", {
      params: { query: { agent: params.agent, status: params.status, limit: params.limit } },
    }),
  );
}

/** `delivery_view()` — derived state, never stored (server/remote_config.py). */
export interface DeliveryView {
  state: string | null;
  delivered_at: string | null;
  channel: string | null;
  attempts: number;
  poisoned: boolean;
  unconfirmed: boolean;
  held: boolean;
  last_error: string | null;
}

/** `GET /config/revisions/{id}` — `proposal_view()`; bodies excluded. */
export interface ProposalView {
  proposal_id: string;
  status: string;
  statuses: string[];
  agents: number;
  config_hash: string | null;
  note: string | null;
  created_at: string | null;
  scope: Record<string, unknown> | null;
  revisions: (ConfigRevision & { delivery: DeliveryView & { current: boolean } })[];
}

export async function getConfigProposal(proposalId: string): Promise<ProposalView> {
  return unwrap<ProposalView>(
    api.GET("/config/revisions/{proposal_id}", {
      params: { path: { proposal_id: proposalId } },
    }),
  );
}

/** `POST …/reject` — kept and marked `rejected`; evidence is never deleted. */
export async function rejectConfigProposal(
  proposalId: string,
  reason: string | null,
): Promise<unknown> {
  return unwrap(
    api.POST("/config/revisions/{proposal_id}/reject", {
      params: { path: { proposal_id: proposalId } },
      body: { reason },
    }),
  );
}

/**
 * `POST …/reactivate` — the only un-poisoning there is: an approver re-runs
 * validation and re-approves. Nothing on the server reactivates on a timer.
 */
export async function reactivateConfigProposal(
  proposalId: string,
  input: { reason: string | null; confirmEndpointChange?: boolean; overrideReason?: string | null },
): Promise<unknown> {
  return unwrap(
    api.POST("/config/revisions/{proposal_id}/reactivate", {
      params: { path: { proposal_id: proposalId } },
      body: {
        reason: input.reason,
        confirm_endpoint_change: input.confirmEndpointChange ?? false,
        override_reason: input.overrideReason ?? null,
      },
    }),
  );
}

/* ------------------------------------------------------------------ */
/* Rollouts                                                            */
/* ------------------------------------------------------------------ */

/** The dry-run plan (`rollout.preview_rollout()`) — also the create body's echo. */
export interface RolloutPlan {
  dry_run: true;
  proposal_id: string;
  config_hash: string | null;
  agents: number;
  batch: {
    count: number;
    canary_percent: number | null;
    max_agents_per_batch: number | null;
    max_percent_per_batch: number | null;
    size: number;
    health_gate_seconds: number;
    ttl_minutes: number;
    restart_after_apply: boolean;
  };
  batches: { batch: number; agents: number; agent_ids: string[] }[];
}

/** Everything `POST /config/rollouts` accepts; `dryRun` makes it a plan only. */
export interface RolloutRequestInput {
  dryRun?: boolean;
  note?: string | null;
  healthGateSeconds?: number | null;
  restartAfterApply?: boolean;
  confirmScopeChange?: boolean;
  confirmUncheckedStructure?: boolean;
  confirmEndpointChange?: boolean;
  overrideReason?: string | null;
}

/**
 * `POST /config/rollouts?proposal_id=…` — with `dryRun: true` this plans and
 * changes nothing (operator question); otherwise it IS the approval (approver).
 */
export async function createConfigRollout(
  proposalId: string,
  input: RolloutRequestInput,
): Promise<RolloutPlan | RolloutView> {
  const result = await unwrap<unknown>(
    api.POST("/config/rollouts", {
      params: { query: { proposal_id: proposalId } },
      body: {
        dry_run: input.dryRun ?? false,
        note: input.note ?? null,
        health_gate_seconds: input.healthGateSeconds ?? null,
        restart_after_apply: input.restartAfterApply ?? false,
        confirm_scope_change: input.confirmScopeChange ?? false,
        confirm_unchecked_structure: input.confirmUncheckedStructure ?? false,
        confirm_endpoint_change: input.confirmEndpointChange ?? false,
        override_reason: input.overrideReason ?? null,
      },
    }),
  );
  if ((result as { dry_run?: boolean }).dry_run) return result as RolloutPlan;
  return result as RolloutView;
}

/** One per-agent rollout state (`pending|delivered|applying|applied|failed|reverted|paused`). */
export type RolloutAgentState =
  | "pending"
  | "delivered"
  | "applying"
  | "applied"
  | "failed"
  | "reverted"
  | "paused";

/** `GET /config/rollouts/{id}` — `rollout.view()`; bodies excluded. */
export interface RolloutView {
  rollout_id: string;
  proposal_id: string | null;
  kind: string | null;
  status: string;
  created_at: string | null;
  expires_at: string | null;
  completed_at: string | null;
  paused_reason: string | null;
  pause_detail: string | null;
  approver: string | null;
  note: string | null;
  config_hash: string | null;
  batch: {
    current: number;
    count: number;
    size: number;
    health_gate_seconds: number | null;
    restart_after_apply: boolean;
    started_at: string | null;
  };
  agents: { total: number; states: Record<string, number> };
  batches: {
    batch: number;
    agents: number;
    status: string;
    states: Record<string, number>;
    agent_ids: string[];
  }[];
}

export async function getConfigRollout(rolloutId: string): Promise<RolloutView> {
  return unwrap<RolloutView>(
    api.GET("/config/rollouts/{rollout_id}", { params: { path: { rollout_id: rolloutId } } }),
  );
}

/** One row of `GET /config/rollouts` (list shape, no batches). */
export interface RolloutSummary {
  rollout_id: string;
  proposal_id: string | null;
  kind: string | null;
  status: string;
  created_at: string | null;
  batch: { current: number; count: number };
  agents: number;
  paused_reason: string | null;
  states: Record<string, number>;
}

export async function listConfigRollouts(
  params: { status?: string; limit?: number } = {},
): Promise<{ rollouts: RolloutSummary[]; count: number; statuses: string[] }> {
  return unwrap(
    api.GET("/config/rollouts", {
      params: { query: { status: params.status, limit: params.limit } },
    }),
  );
}

/** `POST …/abort` — stops promotion; already-delivered configs are not recalled. */
export async function abortConfigRollout(rolloutId: string, reason: string | null): Promise<unknown> {
  return unwrap(
    api.POST("/config/rollouts/{rollout_id}/abort", {
      params: { path: { rollout_id: rolloutId } },
      body: { reason },
    }),
  );
}

/** `POST …/resume` — clears a pause; the gate re-runs, nothing is re-delivered. */
export async function resumeConfigRollout(rolloutId: string, reason: string | null): Promise<unknown> {
  return unwrap(
    api.POST("/config/rollouts/{rollout_id}/resume", {
      params: { path: { rollout_id: rolloutId } },
      body: { reason },
    }),
  );
}

/** `POST …/rollback` — a NEW gated rollout, canary first; never automatic. */
export async function rollbackConfigRollout(rolloutId: string, note: string | null): Promise<unknown> {
  return unwrap(
    api.POST("/config/rollouts/{rollout_id}/rollback", {
      params: { path: { rollout_id: rolloutId } },
      body: { note },
    }),
  );
}
