import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  abortConfigRollout,
  getConfigRollout,
  resumeConfigRollout,
  rollbackConfigRollout,
  type RolloutView,
} from "../../api/config";
import { ApiError } from "../../api/errors";
import { roleAtLeast } from "../../lib/roles";
import { StatusBadge } from "../StatusBadge";
import { Modal } from "../Modal";
import { useSession } from "../../api/session";

/**
 * Why a rollout paused — the operator's next action differs per reason, so
 * "paused" alone is not an answer (Streamlit parity, `5_Remote_Config.py`).
 * `config_conflict` is the one fixable-on-the-agent pause: resume re-runs the
 * gate; `failed`/`auto_reverted`/`superseded` are terminal (reactivate or a
 * rollback proposal instead).
 */
const DIALOG_TITLES: Record<string, string> = {
  abort: "Abort promotion?",
  resume: "Resume this rollout?",
  rollback: "Roll back applied agents?",
};

const PAUSE_HELP: Record<string, string> = {
  failed: "An agent reported FAILED. The revision is poisoned and will not be retried.",
  auto_reverted: "An agent restored its last working config — the supervisor refused this one.",
  agent_went_silent: "An agent stopped reporting after delivery, so its health cannot be verified.",
  unhealthy: "An agent reports healthy=false after being delivered this config.",
  config_conflict:
    "The agent applied the config, but its host merges a second config source scheduling the same queries. Fix it on the agent host, then resume — the revision itself is fine.",
  delivery_unconfirmed: "An agent never acknowledged its delivery (attempt budget exhausted).",
  superseded: "Another revision replaced this one on at least one agent.",
  ttl_expired: "Nobody acted within the rollout TTL, so it was cancelled.",
  aborted: "An operator aborted the rollout. Already-delivered configs were not recalled.",
};

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/**
 * Live rollout view: batch progress, per-batch agent states, the pause reason
 * and the three stop decisions — abort (operator, stops promoting), resume
 * (approver, clears a fixable pause), rollback (approver, a NEW gated rollout).
 * Never automatic, never a retry: the server poisons failures and a paused
 * rollout stays paused until a human decides.
 */
export function RolloutMonitor({ rolloutId }: { rolloutId: string }) {
  const session = useSession();
  const role = session.data?.role ?? null;
  const canOperator = roleAtLeast(role, "operator");
  const canApprover = roleAtLeast(role, "approver");
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"abort" | "resume" | "rollback" | null>(null);
  const [reason, setReason] = useState("");

  const rollout = useQuery({
    queryKey: ["config-rollout", rolloutId],
    queryFn: () => getConfigRollout(rolloutId),
    refetchInterval: (query) =>
      query.state.data && ["running", "pending", "paused"].includes(query.state.data.status)
        ? 5_000
        : false,
  });

  function act(fn: () => Promise<unknown>) {
    setError(null);
    fn()
      .then(() => {
        setDialog(null);
        setReason("");
        queryClient.invalidateQueries({ queryKey: ["config-rollout", rolloutId] });
        queryClient.invalidateQueries({ queryKey: ["config-rollouts"] });
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "The action failed.");
      });
  }

  if (rollout.isError) {
    return <p className="text-sm text-[var(--color-danger)]">Rollout {rolloutId} could not be loaded.</p>;
  }
  if (!rollout.data) {
    return <p className="text-sm text-[var(--color-muted)]">Loading rollout…</p>;
  }
  const view: RolloutView = rollout.data;
  const paused = view.status === "paused";
  const resumable = paused && !["failed", "auto_reverted", "superseded"].includes(view.paused_reason ?? "");

  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <span>
          <StatusBadge status={view.status} />{" "}
          <span className="ml-1 font-mono text-xs">{view.rollout_id}</span>
        </span>
        <span className="text-[var(--color-muted)]">
          Batch {Math.min(view.batch.current + 1, view.batch.count)}/{view.batch.count}
        </span>
        <span className="text-[var(--color-muted)]">
          {view.agents.states.applied ?? 0} applied ·{" "}
          {(view.agents.states.failed ?? 0) + (view.agents.states.reverted ?? 0)} failed
        </span>
        <span className="text-[var(--color-muted)]">
          Gate {view.batch.health_gate_seconds ?? "?"}s · started {formatWhen(view.batch.started_at)}
        </span>
      </div>

      {paused && (
        <div className="rounded-md border border-[var(--color-warn)] bg-[color-mix(in_srgb,var(--color-warn)_12%,transparent)] p-3 text-xs">
          <p>
            <strong>Paused: {view.paused_reason}.</strong> {PAUSE_HELP[view.paused_reason ?? ""] ?? ""}
          </p>
          <p className="mt-1 text-[var(--color-muted)]">
            {view.agents.states.applied ?? 0} applied, {view.agents.states.failed ?? 0} failed at batch{" "}
            {view.batch.current + 1}. Nothing is retried and nothing is rolled back automatically — decide below.
          </p>
          {view.pause_detail && <p className="mt-1 break-all font-mono">{view.pause_detail}</p>}
        </div>
      )}
      {view.status === "completed" && (
        <p className="text-xs text-[var(--color-ok)]">
          Every agent applied the config and stayed healthy through its gate.
        </p>
      )}
      {view.status === "aborted" && (
        <p className="text-xs text-[var(--color-warn)]">
          Aborted. Configs already delivered were <strong>not</strong> recalled.
        </p>
      )}
      {view.status === "cancelled" && (
        <p className="text-xs text-[var(--color-warn)]">
          Cancelled ({view.paused_reason}). {PAUSE_HELP[view.paused_reason ?? ""] ?? ""}
        </p>
      )}

      <table className="w-full text-xs">
        <thead className="text-left text-[var(--color-muted)]">
          <tr>
            <th className="py-1 pr-4 font-medium">Batch</th>
            <th className="py-1 pr-4 font-medium">Agents</th>
            <th className="py-1 pr-4 font-medium">Status</th>
            <th className="py-1 font-medium">States</th>
          </tr>
        </thead>
        <tbody>
          {view.batches.map((batch) => (
            <tr key={batch.batch} className="border-t border-[var(--color-border)]">
              <td className="py-1.5 pr-4">#{batch.batch + 1}</td>
              <td className="py-1.5 pr-4">{batch.agents}</td>
              <td className="py-1.5 pr-4">
                <StatusBadge status={batch.status} />
              </td>
              <td className="py-1.5 text-[var(--color-muted)]">
                {Object.entries(batch.states)
                  .map(([state, count]) => `${count} ${state}`)
                  .join(" · ") || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={!canOperator || !["running", "paused"].includes(view.status)}
          onClick={() => setDialog("abort")}
          className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-bg)] disabled:opacity-40"
        >
          Abort promotion
        </button>
        {resumable && (
          <button
            type="button"
            disabled={!canApprover}
            onClick={() => setDialog("resume")}
            className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-bg)] disabled:opacity-40"
          >
            Resume
          </button>
        )}
        <button
          type="button"
          disabled={!canApprover || (view.agents.states.applied ?? 0) === 0 || view.kind === "rollback"}
          onClick={() => setDialog("rollback")}
          className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-bg)] disabled:opacity-40"
        >
          Roll back applied agents
        </button>
      </div>
      <p className="text-[11px] text-[var(--color-muted)]">
        Abort stops promotion and cannot recall a delivered config. Roll back creates a new revision per
        applied agent and runs it as its own gated rollout — a different, bigger decision.
      </p>

      <Modal open={dialog !== null} title={DIALOG_TITLES[dialog ?? "abort"] ?? ""} onClose={() => setDialog(null)}>
        {dialog === "rollback" && (
          <p className="mb-3 text-xs text-[var(--color-warn)]">
            This rolls back every agent this rollout applied — canary first, gated like any other rollout.
          </p>
        )}
        {dialog === "abort" && (
          <p className="mb-3 text-xs text-[var(--color-muted)]">
            Un-promoted agents stay held; applied agents are untouched. Already-delivered configs are not recalled.
          </p>
        )}
        {dialog === "resume" && (
          <p className="mb-3 text-xs text-[var(--color-muted)]">
            Nothing is re-delivered; the gate re-runs on the next tick and pauses again if the cause is still there.
          </p>
        )}
        <label className="mb-1 block text-xs" htmlFor="rollout-reason">
          {dialog === "abort" ? "Abort reason (audit log)" : dialog === "resume" ? "What changed on the agent? (audit log)" : "Note (audit log)"}
        </label>
        <input
          id="rollout-reason"
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="mb-4 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setDialog(null)}
            className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs"
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid={`confirm-${dialog}`}
            onClick={() => {
              if (dialog === "abort") act(() => abortConfigRollout(rolloutId, reason || null));
              else if (dialog === "resume") act(() => resumeConfigRollout(rolloutId, reason || null));
              else if (dialog === "rollback") act(() => rollbackConfigRollout(rolloutId, reason || null));
            }}
            className="rounded-md border border-[var(--color-danger)] px-3 py-1.5 text-xs font-medium text-[var(--color-danger)] hover:bg-[color-mix(in_srgb,var(--color-danger)_15%,transparent)]"
          >
            {dialog === "abort" ? "Abort promotion" : dialog === "resume" ? "Resume" : "Roll back"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

