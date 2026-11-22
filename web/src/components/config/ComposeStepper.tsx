import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { pushConfig, validateConfig, type Verdict } from "../../api/config";
import { listGroups, type GroupView } from "../../api/groups";
import { listAgents, getAgent } from "../../api/endpoints";
import { ApiError } from "../../api/errors";
import { roleAtLeast } from "../../lib/roles";
import { configObjects } from "../../lib/configText";
import { Diff } from "./Diff";
import { RolloutMonitor } from "./RolloutMonitor";
import { StatusBadge } from "../StatusBadge";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Checkbox";
import { Notice } from "../ui/Toast";
import { useSession } from "../../api/session";

/**
 * The compose stepper: **target → edit → validate → push**.
 *
 * There is no proposal step in the operator's way. `POST /config/push` validates,
 * activates the revision and stages the rollout in one action; the immutable
 * revision and its `approved_by` are still recorded, so history and rollback are
 * unchanged. The push needs the `approver` role — the single button is the approval.
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

  // Step 3–4 — review and push
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [confirmScopeChange, setConfirmScopeChange] = useState(false);
  const [confirmUnchecked, setConfirmUnchecked] = useState(false);
  const [gate, setGate] = useState<number | null>(null);
  const [canaryPercent, setCanaryPercent] = useState<number | null>(null);
  const [maxAgents, setMaxAgents] = useState<number | null>(null);
  const [maxPercent, setMaxPercent] = useState<number | null>(null);
  const [restart, setRestart] = useState(false);
  const [rolloutId, setRolloutId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const agents = useQuery({ queryKey: ["agents", {}], queryFn: () => listAgents({}) });
  const groups = useQuery({ queryKey: ["groups"], queryFn: listGroups, enabled: targetMode === "group" });

  // `selectedGroup` must stay above `diffAgentId`: `targetIds()` is called during
  // render, and reading a `const` declared later is a TDZ ReferenceError — which is
  // exactly what "switch to saved group" used to throw.
  const selectedGroup: GroupView | undefined = groups.data?.groups.find(
    (g) => g.name === groupName,
  );

  function targetIds(): string[] {
    if (targetMode === "group") return selectedGroup?.members ?? [];
    return agents.data?.agents.filter((a) => selectedIds.has(a.id)).map((a) => a.id) ?? [];
  }

  // The agent whose live config the diff compares against: the prefilled one,
  // or the single selected agent — a diff against a moving set is not a review.
  const diffAgentId = prefillId || (targetIds().length === 1 ? targetIds()[0] : "");
  const diffAgent = useQuery({
    queryKey: ["agent", diffAgentId],
    queryFn: () => getAgent(diffAgentId),
    enabled: diffAgentId !== "",
  });

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
    setConfirmScopeChange(false);
    setConfirmUnchecked(false);
    setRolloutId(null);
    setError(null);
  }

  function pushInput() {
    return {
      configMap: configMap(),
      scope: targetMode === "group" ? { group: groupName! } : { agents: ids },
      note: null,
      confirmScopeChange,
      confirmUncheckedStructure: confirmUnchecked,
      canaryPercent,
      maxAgentsPerBatch: maxAgents,
      maxPercentPerBatch: maxPercent,
      healthGateSeconds: gate,
      restartAfterApply: restart,
    };
  }

  const validate = useMutation({
    mutationFn: () => validateConfig(configMap(), ids[0] ?? null),
    onSuccess: (v) => {
      setVerdict(v);
      setError(null);
    },
    onError: (err: unknown) => setError(err instanceof ApiError ? err.message : "Validation failed."),
  });

  const push = useMutation({
    mutationFn: () => pushConfig(pushInput()),
    onSuccess: (result) => {
      if (!("rollout_id" in result)) {
        setError("The server did not start a rollout.");
        return;
      }
      setRolloutId(result.rollout_id);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["config-revisions"] });
      queryClient.invalidateQueries({ queryKey: ["config-rollouts"] });
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError && err.status === 409) {
        // A group whose membership shrank needs the acknowledgement; surface it with
        // the server's own words instead of hiding the checkbox.
        setConfirmScopeChange(true);
      }
      setError(err instanceof ApiError ? err.message : "The push was refused.");
    },
  });

  const unchecked = verdict?.structure_checked === false;
  const pushDisabled =
    !body.trim() ||
    bodyIsRedacted ||
    ids.length === 0 ||
    !canApprover ||
    (unchecked && !confirmUnchecked) ||
    push.isPending;

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
                resetAfterAction();
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
                {(agents.data?.agents ?? []).map((agent) => {
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
                            resetAfterAction();
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
                resetAfterAction();
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
            data-testid="validate-config"
          >
            {validate.isPending ? "Validating…" : "Validate"}
          </Button>
          <span className="text-xs text-[var(--color-muted)]">
            The #100 pipeline, no storage — validating changes nothing.
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

      {/* ---- 4. Push ---- */}
      <section className={stepClass}>
        <h2 className={stepTitle}>4. Push</h2>
        <p className="mb-3 text-xs text-[var(--color-muted)]">
          One action: validate, activate the revision and release the canary only. The rest of the fleet
          is promoted one batch per health gate. No proposal to approve first.
        </p>

        <div className="mb-3 flex flex-wrap items-end gap-3 text-xs">
          <label className="flex flex-col gap-1">
            Canary %
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              value={canaryPercent ?? ""}
              onChange={(e) => setCanaryPercent(e.target.value === "" ? null : Number(e.target.value))}
              placeholder="auto"
              className="field w-36"
              data-testid="canary-percent"
            />
          </label>
          <label className="flex flex-col gap-1">
            Max agents / batch
            <input
              type="number"
              min={1}
              step={1}
              value={maxAgents ?? ""}
              onChange={(e) => setMaxAgents(e.target.value === "" ? null : Number(e.target.value))}
              placeholder="auto"
              className="field w-36"
            />
          </label>
          <label className="flex flex-col gap-1">
            Max % / batch
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              value={maxPercent ?? ""}
              onChange={(e) => setMaxPercent(e.target.value === "" ? null : Number(e.target.value))}
              placeholder="auto"
              className="field w-36"
              data-testid="max-percent"
            />
          </label>
          <label className="flex flex-col gap-1">
            Health gate (seconds)
            <input
              type="number"
              min={0}
              max={86400}
              step={5}
              value={gate ?? ""}
              onChange={(e) => setGate(e.target.value === "" ? null : Number.parseInt(e.target.value, 10))}
              placeholder="auto"
              className="field w-36"
              data-testid="health-gate"
            />
          </label>
          <Checkbox
            label="Restart each agent once its batch passes the gate"
            checked={restart}
            onChange={setRestart}
            className="pb-1"
          />
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            data-testid="push-config"
            disabled={pushDisabled}
            onClick={() => push.mutate()}
          >
            {push.isPending ? "Pushing…" : `Push to ${ids.length} agent(s)`}
          </Button>
          {!canApprover && (
            <span className="text-xs text-[var(--color-warn)]">
              Pushing needs the <strong>approver</strong> role. You are {role}.
            </span>
          )}
        </div>

        {unchecked && (
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
        {confirmScopeChange && targetMode === "group" && (
          <Checkbox
            className="mb-3 items-start"
            label={
              <span className="max-w-prose">
                Confirm the membership change this group produced (the server asked for it because it
                resolved smaller or larger than last time)
              </span>
            }
            checked={confirmScopeChange}
            onChange={setConfirmScopeChange}
          />
        )}
        <p className="text-xs text-[var(--color-muted)]">
          The push records who approved it and releases the canary only; stop and roll back stay separate
          decisions in Rollouts.
        </p>
      </section>

      {/* ---- 5. Monitor ---- */}
      {rolloutId && (
        <section className={stepClass}>
          <h2 className={stepTitle}>5. Roll out</h2>
          <RolloutMonitor rolloutId={rolloutId} />
        </section>
      )}

      {error && (
        <div data-testid="stepper-error" role="alert">
          <Notice kind="error">{error}</Notice>
        </div>
      )}
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
        <p className="text-[var(--color-danger)]">Validation failed. Fix these before pushing:</p>
      )}
      {(verdict.details ?? []).map((finding, i) => (
        <p key={i} className={finding.severity === "error" ? "text-[var(--color-danger)]" : "text-[var(--color-warn)]"}>
          <span className="font-mono">{finding.path}</span> — {finding.message}
        </p>
      ))}
      {(verdict.errors ?? []).map((e, i) => (
        <p key={`e${i}`} className="text-[var(--color-danger)]">
          {e}
        </p>
      ))}
      {(verdict.warnings ?? []).map((w, i) => (
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
 * would bury the real change, and echoing them into a push is forbidden.
 */
function diffText(effectiveConfig: unknown, key: string): string {
  const objects = configObjects(effectiveConfig);
  const own = objects.find((o) => o.key === key) ?? objects[0];
  return own?.body ?? "";
}
