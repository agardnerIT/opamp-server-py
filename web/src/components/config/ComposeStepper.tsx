import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  createConfigRollout,
  getConfigProposal,
  proposeConfig,
  rejectConfigProposal,
  validateConfig,
  type ProposeResult,
  type RolloutPlan,
  type Verdict,
} from "../../api/config";
import { listGroups, type GroupView } from "../../api/groups";
import { listAgents, getAgent, type AgentEntry } from "../../api/endpoints";
import { ApiError } from "../../api/errors";
import { roleAtLeast } from "../../lib/roles";
import { configObjects } from "../../lib/configText";
import { Diff } from "./Diff";
import { RolloutMonitor } from "./RolloutMonitor";
import { Modal } from "../Modal";
import { StatusBadge } from "../StatusBadge";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Checkbox";
import { Notice } from "../ui/Toast";
import { useSession } from "../../api/session";

/**
 * The compose stepper (Streamlit `5_Remote_Config.py` parity, built around
 * review before action — never around a big red Push button):
 *
 *   1 Target   who? saved group (resolved by the server again) or explicit ids
 *   2 Edit     one config object, prefilled decoded from an agent's report
 *   3 Review   the #100 pipeline verdict + a diff against the agent's config
 *   4 Propose  stores immutable revisions, delivers NOTHING
 *   5 Approve  dry-run plan, then create the rollout — which IS the approval
 *
 * Three rules the server cannot save us from: a `<redacted>` body is never
 * prefilled or sent (#98); abort and rollback stay visibly different; a paused
 * rollout shows its one reason (RolloutMonitor).
 */

const CONTENT_TYPES = ["text/yaml", "application/json"] as const;

export function ComposeStepper() {
  const session = useSession();
  const role = session.data?.role ?? null;
  const canOperator = roleAtLeast(role, "operator");
  const canApprover = roleAtLeast(role, "approver");
  const queryClient = useQueryClient();

  // Step 1 — target
  const [targetMode, setTargetMode] = useState<"group" | "agents">("agents");
  const [groupName, setGroupName] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Step 2 — edit
  const [contentType, setContentType] = useState<string>("text/yaml");
  const [objectKey, setObjectKey] = useState("");
  const [body, setBody] = useState("");
  const [prefillId, setPrefillId] = useState<string>("");
  const [prefillNote, setPrefillNote] = useState<{ tone: "info" | "warn"; text: string } | null>(null);

  // Step 3–5
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [proposal, setProposal] = useState<ProposeResult | null>(null);
  const [confirmScopeChange, setConfirmScopeChange] = useState(false);
  const [confirmUnchecked, setConfirmUnchecked] = useState(false);
  const [plan, setPlan] = useState<RolloutPlan | null>(null);
  const [gate, setGate] = useState<number | null>(null);
  const [restart, setRestart] = useState(false);
  const [rolloutId, setRolloutId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  const agents = useQuery({ queryKey: ["agents", {}], queryFn: () => listAgents({}) });
  const groups = useQuery({ queryKey: ["groups"], queryFn: listGroups, enabled: targetMode === "group" });

  // The agent whose live config the diff compares against: the prefilled one,
  // or the single selected agent — a diff against a moving set is not a review.
  const diffAgentId = prefillId || (targetIds().length === 1 ? targetIds()[0] : "");
  const diffAgent = useQuery({
    queryKey: ["agent", diffAgentId],
    queryFn: () => getAgent(diffAgentId),
    enabled: diffAgentId !== "",
  });

  const selectedGroup: GroupView | undefined = groups.data?.groups.find(
    (g) => g.name === groupName,
  );

  function targetIds(): string[] {
    if (targetMode === "group") return selectedGroup?.members ?? [];
    return agents.data?.agents.filter((a) => selectedIds.has(a.id)).map((a) => a.id) ?? [];
  }

  function configMap(): Record<string, unknown> {
    const entry: Record<string, unknown> = { body, content_type: contentType };
    return { [objectKey.trim()]: entry };
  }

  const bodyIsRedacted = body.includes("<redacted>");
  const ids = targetIds();

  function prefill() {
    setPrefillNote(null);
    const detail = diffAgent.data;
    if (!detail) return;
    if (detail.effective_config_redacted) {
      setPrefillNote({
        tone: "warn",
        text:
          "That agent's reported config is redacted (<redacted> markers). It is deliberately not loaded: " +
          "pushing a redacted body back would wipe the fleet's credentials. Read it with " +
          "`opampctl agents config get --reveal` as an approver.",
      });
      return;
    }
    const objects = configObjects(detail.effective_config);
    const own = objects.find((o) => o.key === objectKey.trim()) ?? objects[0];
    if (!own) {
      setPrefillNote({ tone: "info", text: "That agent has not reported a readable config yet." });
      return;
    }
    setBody(own.body);
    if (own.contentType && CONTENT_TYPES.includes(own.contentType as (typeof CONTENT_TYPES)[number])) {
      setContentType(own.contentType);
    }
    setObjectKey(own.key);
    setPrefillNote({
      tone: "info",
      text: `Loaded object "${own.key || "<empty key>"}" from ${detail.id} as ${own.contentType ?? "text/yaml"}, decoded.`,
    });
  }

  function resetAfterAction() {
    setVerdict(null);
    setProposal(null);
    setPlan(null);
    setGate(null);
    setConfirmScopeChange(false);
    setConfirmUnchecked(false);
    setRolloutId(null);
    setError(null);
  }

  const validate = useMutation({
    mutationFn: () => validateConfig(configMap(), ids[0] ?? null),
    onSuccess: (v) => {
      setVerdict(v);
      setError(null);
    },
    onError: (err: unknown) => setError(err instanceof ApiError ? err.message : "Validation failed."),
  });

  const propose = useMutation({
    mutationFn: () =>
      proposeConfig({
        configMap: configMap(),
        scope: targetMode === "group" ? { group: groupName! } : { agents: ids },
        note: null,
        confirmScopeChange,
      }),
    onSuccess: (result) => {
      setProposal(result);
      setVerdict(null);
      setPlan(null);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["config-revisions"] });
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError && err.status === 409) {
        // Scope shrank between save and propose: the server requires the
        // acknowledgement; show it with the server's own words.
        setConfirmScopeChange(true);
      }
      setError(err instanceof ApiError ? err.message : "The proposal was refused.");
    },
  });

  const dryRun = useMutation({
    mutationFn: (gateSeconds: number | null) =>
      createConfigRollout(proposal!.proposal_id, { dryRun: true, healthGateSeconds: gateSeconds }),
    onSuccess: (result) => {
      if ("dry_run" in result) {
        setPlan(result);
        setGate((current) => current ?? result.batch.health_gate_seconds);
        setError(null);
      }
    },
    onError: (err: unknown) => setError(err instanceof ApiError ? err.message : "Planning failed."),
  });

  const approve = useMutation({
    mutationFn: () =>
      createConfigRollout(proposal!.proposal_id, {
        healthGateSeconds: gate,
        restartAfterApply: restart,
        confirmUncheckedStructure: confirmUnchecked,
        confirmScopeChange,
      }),
    onSuccess: (result) => {
      if (!("batches" in result) || !("rollout_id" in result)) {
        setError("The server did not start a rollout.");
        return;
      }
      setRolloutId(result.rollout_id);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["config-revisions"] });
      queryClient.invalidateQueries({ queryKey: ["config-rollouts"] });
    },
    onError: (err: unknown) => setError(err instanceof ApiError ? err.message : "The rollout was refused."),
  });

  const reject = useMutation({
    mutationFn: () => rejectConfigProposal(proposal!.proposal_id, rejectReason || null),
    onSuccess: () => {
      setRejectOpen(false);
      setRejectReason("");
      resetAfterAction();
      queryClient.invalidateQueries({ queryKey: ["config-revisions"] });
    },
    onError: (err: unknown) => setError(err instanceof ApiError ? err.message : "The reject failed."),
  });

  const unchecked = proposal?.revisions.length ? verdict?.structure_checked === false : false;
  const proposalDetail = useQuery({
    queryKey: ["config-proposal", proposal?.proposal_id],
    queryFn: () => getConfigProposal(proposal!.proposal_id),
    enabled: !!proposal && unchecked,
  });

  const stepClass = "card p-4";
  const stepTitle = "mb-3 text-sm font-semibold";

  return (
    <div className="flex flex-col gap-4" data-testid="compose-stepper">
      {/* ---- 1. Target ---- */}
      <section className={stepClass}>
        <h2 className={stepTitle}>1. Target</h2>
        <div className="mb-3 flex gap-2 text-xs">
          {(["agents", "group"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => {
                setTargetMode(mode);
                setVerdict(null);
                setProposal(null);
                setPlan(null);
              }}
              aria-pressed={targetMode === mode}
              className={
                "inline-flex h-7 items-center rounded-[var(--radius-sm)] border px-3 text-xs font-medium transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] " +
                (targetMode === mode
                  ? "border-[var(--color-accent)]/50 bg-[var(--color-accent-soft)] text-[var(--color-accent-strong)]"
                  : "border-[var(--color-border)] text-[var(--color-muted)] hover:text-[var(--color-text)]")
              }
            >
              {mode === "agents" ? "Ad-hoc selection" : "Saved group"}
            </button>
          ))}
        </div>

        {targetMode === "agents" ? (
          <div className="card-2 max-h-64 overflow-auto">
            <table className="data-table">
              <thead className="sticky top-0 z-10 bg-[var(--color-surface-2)]">
                <tr>
                  <th className="w-8" aria-label="Select" />
                  <th>Agent</th>
                  <th>Name</th>
                  <th>Health</th>
                  <th>Config</th>
                  <th>Accepts config</th>
                </tr>
              </thead>
              <tbody>
                {(agents.data?.agents ?? []).map((agent: AgentEntry) => {
                  const accepts = (agent.capabilities & 0x2) !== 0;
                  return (
                    <tr key={agent.id}>
                      <td>
                        <Checkbox
                          label={<span className="sr-only">Select {agent.name ?? agent.id}</span>}
                          checked={selectedIds.has(agent.id)}
                          onChange={(next) => {
                            const nextIds = new Set(selectedIds);
                            if (next) nextIds.add(agent.id);
                            else nextIds.delete(agent.id);
                            setSelectedIds(nextIds);
                            setVerdict(null);
                            setProposal(null);
                          }}
                        />
                      </td>
                      <td className="font-mono text-xs">{agent.id}</td>
                      <td className="text-xs">{agent.name ?? "—"}</td>
                      <td>
                        <StatusBadge
                          tone={agent.healthy === true ? "ok" : agent.healthy === false ? "danger" : "muted"}
                          label={agent.healthy === true ? "Healthy" : agent.healthy === false ? "Unhealthy" : "Unknown"}
                        />
                      </td>
                      <td>
                        <StatusBadge status={agent.remote_config_status} />
                      </td>
                      <td className="text-xs">{accepts ? "yes" : "no"}</td>
                    </tr>
                  );
                })}
                {(agents.data?.agents.length ?? 0) === 0 && (
                  <tr>
                    <td colSpan={6} className="py-3 text-center text-xs text-[var(--color-muted)]">
                      No agents connected.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        ) : (
          <div>
            <select
              aria-label="Saved group"
              value={groupName ?? ""}
              onChange={(e) => {
                setGroupName(e.target.value || null);
                setVerdict(null);
                setProposal(null);
                setPlan(null);
              }}
              className="field max-w-sm"
            >
              <option value="">Choose a saved group…</option>
              {(groups.data?.groups ?? []).map((g) => (
                <option key={g.group_id} value={g.name}>
                  {g.name} — {g.member_count} member(s)
                </option>
              ))}
            </select>
            {groups.data && (groups.data.groups.length ?? 0) === 0 && (
              <p className="mt-2 text-xs text-[var(--color-muted)]">
                No saved groups yet — save a query on the Groups page.
              </p>
            )}
            {selectedGroup && (
              <div className="mt-3 text-xs">
                <p className="font-mono text-[var(--color-muted)]">{selectedGroup.query ?? ""}</p>
                {selectedGroup.health_defined.length > 0 && (
                  <p className="mt-2 text-[var(--color-warn)]">
                    This group is defined on {selectedGroup.health_defined.join(", ")}, so it drops the
                    agents that break — exactly the ones a config push is usually meant to fix. Edit the
                    query on the Groups page instead of forcing this push.
                  </p>
                )}
                {selectedGroup.member_count === 0 && (
                  <p className="mt-2 text-[var(--color-warn)]">
                    This group has zero members right now — a push that ships nothing is refused.
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        <p className="mt-3 text-xs text-[var(--color-muted)]">
          {ids.length} agent(s) in scope.{" "}
          {ids.length > 0 && (
            <span className="font-mono">
              {ids.slice(0, 3).join(", ")}
              {ids.length > 3 ? ` … (+${ids.length - 3})` : ""}
            </span>
          )}
        </p>
      </section>

      {/* ---- 2. Edit ---- */}
      <section className={stepClass}>
        <h2 className={stepTitle}>2. Edit</h2>
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs">
            Content type
            <select
              value={contentType}
              onChange={(e) => setContentType(e.target.value)}
              className="field"
            >
              {CONTENT_TYPES.map((ct) => (
                <option key={ct}>{ct}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Config object name
            <input
              type="text"
              value={objectKey}
              onChange={(e) => setObjectKey(e.target.value)}
              placeholder="empty for an unnamed object (collector)"
              className="field w-64"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Prefill from agent
            <select
              value={prefillId}
              onChange={(e) => setPrefillId(e.target.value)}
              className="field w-64"
            >
              <option value="">(none)</option>
              {(agents.data?.agents ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name ?? a.id}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" onClick={prefill} disabled={!prefillId || !diffAgent.data}>
            Prefill from this agent
          </Button>
        </div>
        {prefillNote && (
          <Notice kind={prefillNote.tone} className="mb-2">
            {prefillNote.text}
          </Notice>
        )}
        <textarea
          aria-label="Config body"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={12}
          spellCheck={false}
          className="field font-mono text-xs leading-relaxed"
        />
        {bodyIsRedacted && (
          <p className="mt-2 text-xs text-[var(--color-danger)]">
            This body contains <code>&lt;redacted&gt;</code>. The server refuses it on purpose: pushing a
            redacted document would wipe every credential in the fleet.
          </p>
        )}
      </section>

      {/* ---- 3. Review ---- */}
      <section className={stepClass}>
        <h2 className={stepTitle}>3. Review</h2>
        <div className="flex items-center gap-3">
          <Button
            disabled={!body.trim() || bodyIsRedacted || ids.length === 0 || !canOperator || validate.isPending}
            onClick={() => validate.mutate()}
          >
            {validate.isPending ? "Validating…" : "Validate"}
          </Button>
          <span className="text-xs text-[var(--color-muted)]">
            The #100 pipeline, no storage — nothing is proposed by validating.
          </span>
        </div>
        {verdict && <VerdictPanel verdict={verdict} />}
        {diffAgentId && diffAgent.data && !diffAgent.data.effective_config_redacted && (
          <div className="mt-3">
            <h3 className="mb-1 text-xs font-medium">
              Diff against {diffAgent.data.name ?? diffAgent.data.id}'s current config
            </h3>
            <Diff oldText={diffText(diffAgent.data.effective_config, objectKey.trim())} newText={body} />
          </div>
        )}
      </section>

      {/* ---- 4. Propose ---- */}
      <section className={stepClass}>
        <h2 className={stepTitle}>4. Propose</h2>
        <p className="mb-3 text-xs text-[var(--color-muted)]">
          A proposal stores one immutable revision per target and delivers nothing. An approver decides
          (or creates the rollout, which is the same decision).
        </p>
        {targetMode === "group" && confirmScopeChange && (
          <Checkbox
            className="mb-2 flex items-start"
            label={<span className="max-w-prose">Confirm the membership change this group produced (required when it resolved smaller)</span>}
            checked={confirmScopeChange}
            onChange={setConfirmScopeChange}
          />
        )}
        <Button
          variant="primary"
          disabled={!body.trim() || bodyIsRedacted || ids.length === 0 || !canOperator || propose.isPending}
          onClick={() => propose.mutate()}
        >
          {propose.isPending ? "Proposing…" : "Propose revision"}
        </Button>
        {proposal && (
          <p className="mt-2 text-xs" data-testid="proposal-status">
            Proposal <span className="font-mono">{proposal.proposal_id}</span> — {proposal.agents} agent(s),
            hash <span className="font-mono">{proposal.config_hash.slice(0, 12)}…</span>, status{" "}
            <StatusBadge status={proposal.status} />
          </p>
        )}
      </section>

      {/* ---- 5. Approve ---- */}
      {proposal && proposal.status === "proposed" && (
        <section className={stepClass}>
          <h2 className={stepTitle}>5. Approve</h2>
          {!canApprover && (
            <p className="mb-2 text-xs text-[var(--color-warn)]">
              Approving and rolling out needs the <strong>approver</strong> role. You are {role}.
            </p>
          )}
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Button
              disabled={dryRun.isPending}
              onClick={() => dryRun.mutate(null)}
            >
              {dryRun.isPending ? "Planning…" : "Plan the rollout (dry run)"}
            </Button>
            <Button onClick={() => setRejectOpen(true)}>
              Reject
            </Button>
          </div>

          {plan && (
            <div className="card-2 mb-3 p-3 text-xs">
              <p className="mb-2 font-medium">Rollout plan (nothing has been delivered)</p>
              <p className="text-[var(--color-muted)]">
                {plan.batch.count} batch(es), canary {plan.batches[0]?.agents ?? 0} agent(s), health gate{" "}
                {plan.batch.health_gate_seconds}s, TTL {plan.batch.ttl_minutes}m
              </p>
              <label className="mt-3 flex flex-col gap-1">
                Health gate (seconds the whole batch must stay green)
                <input
                  type="number"
                  min={0}
                  max={86400}
                  step={5}
                  value={gate ?? plan.batch.health_gate_seconds}
                  onChange={(e) => setGate(Number.parseInt(e.target.value, 10))}
                  className="field w-32"
                />
              </label>
              {gate !== null && gate !== plan.batch.health_gate_seconds && (
                <p className="mt-1 text-[var(--color-warn)]">
                  The plan used {plan.batch.health_gate_seconds}s. Re-plan to check it at {gate}s before
                  approving — the batches do not change, the waiting does.
                </p>
              )}
              <Checkbox
                className="mt-3"
                label="Restart each agent once its batch passes the gate"
                checked={restart}
                onChange={setRestart}
              />
              {gate !== null && (
                <Button
                  size="sm"
                  className="mt-2"
                  onClick={() => dryRun.mutate(gate)}
                >
                  Re-plan with this gate
                </Button>
              )}
            </div>
          )}

          {proposalDetail.data && proposalDetail.data.revisions.some((r) => r.verdict?.structure_checked === false) && (
            <Checkbox
              className="mb-3 items-start"
              label={
                <span className="max-w-prose text-[var(--color-warn)]">
                  <strong>No structural checks ran for this agent type</strong> — validation covered the
                  bytes and the object name, nothing about what the agent will do with the document. A
                  config that stops it starting cannot be noticed here. (Recorded in the audit log.)
                </span>
              }
              checked={confirmUnchecked}
              onChange={setConfirmUnchecked}
            />
          )}

          <Button
            variant="primary"
            data-testid="approve-rollout"
            disabled={!canApprover || (unchecked && !confirmUnchecked) || approve.isPending}
            onClick={() => approve.mutate()}
          >
            {approve.isPending ? "Creating rollout…" : "Approve & roll out"}
          </Button>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            Creates the rollout: approves the proposal, releases the canary only.
          </p>
        </section>
      )}

      {/* ---- 6. Monitor ---- */}
      {rolloutId && (
        <section className={stepClass}>
          <h2 className={stepTitle}>6. Roll out</h2>
          <RolloutMonitor rolloutId={rolloutId} />
        </section>
      )}

      {error && (
        <div data-testid="stepper-error" role="alert">
          <Notice kind="error">{error}</Notice>
        </div>
      )}

      <Modal open={rejectOpen} title="Reject this proposal?" onClose={() => setRejectOpen(false)}>
        <p className="mb-3 text-xs text-[var(--color-muted)]">
          The revision is kept and marked <code>rejected</code> — evidence is never deleted.
        </p>
        <label className="mb-1 block text-xs" htmlFor="reject-reason">
          Reason (audit log)
        </label>
        <input
          id="reject-reason"
          type="text"
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          className="field mb-4"
        />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setRejectOpen(false)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            data-testid="confirm-reject"
            onClick={() => reject.mutate()}
          >
            Reject
          </Button>
        </div>
      </Modal>
    </div>
  );
}

/** The verdict panel: validity, findings, policy gate, profile attribution. */
function VerdictPanel({ verdict }: { verdict: Verdict }) {
  const policy = verdict.policy ?? {};
  return (
    <div className="mt-3 text-xs" data-testid="verdict-panel">
      {verdict.profile && (
        <p className="mb-1 text-[var(--color-muted)]">
          Judged under the <span className="font-mono">{verdict.profile}</span> profile (
          {verdict.profile_source ?? "unknown"}).
        </p>
      )}
      {verdict.valid ? (
        <p className="text-[var(--color-ok)]">
          Valid — {verdict.objects ?? 1} object(s), {verdict.size_bytes ?? 0} bytes, hash{" "}
          <span className="font-mono">{verdict.config_hash?.slice(0, 12)}…</span>
        </p>
      ) : (
        <p className="text-[var(--color-danger)]">Validation failed. Fix these before proposing:</p>
      )}
      {verdict.details.map((finding, i) => (
        <p key={i} className={finding.severity === "error" ? "text-[var(--color-danger)]" : "text-[var(--color-warn)]"}>
          <span className="font-mono">{finding.path}</span> — {finding.message}
        </p>
      ))}
      {verdict.errors.map((e, i) => (
        <p key={`e${i}`} className="text-[var(--color-danger)]">
          {e}
        </p>
      ))}
      {verdict.warnings.map((w, i) => (
        <p key={`w${i}`} className="text-[var(--color-warn)]">
          {w}
        </p>
      ))}
      {policy.status === "violations" ? (
        <p className="text-[var(--color-danger)]">The policy gate rejected this config.</p>
      ) : policy.status === "skipped" ? (
        <p className="text-[var(--color-muted)]">OPA is not enabled, so the policy gate was skipped — not passed.</p>
      ) : null}
    </div>
  );
}

/**
 * The diff baseline: the agent's reported object with the same key (or the
 * first one). A redacted report never becomes a baseline — the sentinel lines
 * would bury the real change, and echoing them into a proposal is forbidden.
 */
function diffText(effectiveConfig: unknown, key: string): string {
  const objects = configObjects(effectiveConfig);
  const own = objects.find((o) => o.key === key) ?? objects[0];
  return own?.body ?? "";
}
