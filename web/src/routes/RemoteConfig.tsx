import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getConfigPosture, listConfigRevisions, listConfigRollouts } from "../api/config";
import { ComposeStepper } from "../components/config/ComposeStepper";
import { RolloutMonitor } from "../components/config/RolloutMonitor";
import { reactivateConfigProposal } from "../api/config";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { Section } from "../components/Section";
import { StatusBadge } from "../components/StatusBadge";

/**
 * Remote Config (new-ui stage 6): compose → review → propose → approve →
 * gated rollout, plus history. Composition lives in `ComposeStepper`; this
 * page owns the posture banner, the tabs and the history tables.
 */

type Tab = "compose" | "rollouts" | "revisions";

const TABS: { id: Tab; label: string }[] = [
  { id: "compose", label: "Compose" },
  { id: "rollouts", label: "Rollouts" },
  { id: "revisions", label: "Revisions" },
];

export default function RemoteConfig() {
  const [tab, setTab] = useState<Tab>("compose");
  const posture = useQuery({ queryKey: ["config-posture"], queryFn: getConfigPosture });

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-lg font-semibold">Remote Config</h1>
        <p className="text-xs text-[var(--color-muted)]">
          Propose → review → approve → canary → gated batches. Nothing is delivered to a whole fleet
          without evidence that the canary came back healthy.
        </p>
      </header>

      {posture.data && <PostureBanner posture={posture.data} />}

      <div className="flex gap-1 border-b border-[var(--color-border)]" role="tablist" aria-label="Remote config views">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={
              "-mb-px rounded-t-md border border-b-0 px-3 py-1.5 text-sm " +
              (tab === t.id
                ? "border-[var(--color-border)] bg-[var(--color-surface)] font-medium"
                : "border-transparent text-[var(--color-muted)] hover:text-[var(--color-text)]")
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "compose" && <ComposeStepper />}
      {tab === "rollouts" && <Rollouts />}
      {tab === "revisions" && <Revisions />}
    </div>
  );
}

/** Push stays refused until every requirement is met — the page says why up front. */
function PostureBanner({ posture }: { posture: Awaited<ReturnType<typeof getConfigPosture>> }) {
  const unmet = posture.unmet ?? [];
  if (unmet.length > 0) {
    return (
      <div className="rounded-md border border-[var(--color-danger)] p-3 text-xs">
        <p>
          <strong>Config push is blocked by the security posture.</strong> Unmet: {unmet.join(", ")}. Push
          stays refused until every requirement is met.
        </p>
      </div>
    );
  }
  if (posture.insecure) {
    return (
      <div className="rounded-md border border-[var(--color-warn)] p-3 text-xs">
        <strong>Config push is enabled on an insecure posture</strong> (no TLS, agent auth or audit). Do
        not use this on a real fleet.
      </div>
    );
  }
  if (!posture.allowed) {
    return (
      <div className="rounded-md border border-[var(--color-border)] p-3 text-xs text-[var(--color-muted)]">
        Config push is <strong>disabled</strong>. Set <code>OPAMP_CONFIG_PUSH_ENABLED=true</code> on a
        server whose posture passes.
      </div>
    );
  }
  return null;
}

function Rollouts() {
  const rollouts = useQuery({
    queryKey: ["config-rollouts"],
    queryFn: () => listConfigRollouts({ limit: 50 }),
    refetchInterval: 10_000,
  });
  const [openId, setOpenId] = useState<string | null>(null);

  if (rollouts.data && rollouts.data.rollouts.length === 0) {
    return (
      <EmptyState
        title="No rollouts yet"
        detail="Compose a proposal first — creating the rollout is the approval."
      />
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <table className="w-full text-xs">
        <thead className="text-left text-[var(--color-muted)]">
          <tr>
            <th className="py-1.5 pr-4 font-medium">Rollout</th>
            <th className="py-1.5 pr-4 font-medium">Status</th>
            <th className="py-1.5 pr-4 font-medium">Batch</th>
            <th className="py-1.5 pr-4 font-medium">Agents</th>
            <th className="py-1.5 pr-4 font-medium">Paused</th>
            <th className="py-1.5 font-medium">Created</th>
          </tr>
        </thead>
        <tbody>
          {(rollouts.data?.rollouts ?? []).map((r) => (
            <tr
              key={r.rollout_id}
              className="cursor-pointer border-t border-[var(--color-border)] hover:bg-[var(--color-bg)]"
              onClick={() => setOpenId(openId === r.rollout_id ? null : r.rollout_id)}
            >
              <td className="py-1.5 pr-4 font-mono">{r.rollout_id}</td>
              <td className="py-1.5 pr-4">
                <StatusBadge status={r.status} />
              </td>
              <td className="py-1.5 pr-4">
                {Math.min(r.batch.current + 1, r.batch.count)}/{r.batch.count}
              </td>
              <td className="py-1.5 pr-4">{r.agents}</td>
              <td className="py-1.5 pr-4">{r.paused_reason ?? "—"}</td>
              <td className="py-1.5">{formatWhen(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {openId && (
        <Section title={`Rollout ${openId}`} caption="Batch-by-batch progress and the stop decisions.">
          <RolloutMonitor rolloutId={openId} />
        </Section>
      )}
    </div>
  );
}

function Revisions() {
  const revisions = useQuery({ queryKey: ["config-revisions"], queryFn: () => listConfigRevisions({}) });
  const [reactivateId, setReactivateId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  function reactivate() {
    if (!reactivateId) return;
    reactivateConfigProposal(reactivateId, { reason: reason || null })
      .then(() => {
        setReactivateId(null);
        setReason("");
        setError(null);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Reactivation was refused."),
      );
  }

  return (
    <div className="flex flex-col gap-3">
      {revisions.data && revisions.data.revisions.length === 0 && (
        <EmptyState title="No revisions yet" detail="Every push, proposal and rollback lands here as an immutable revision row." />
      )}
      <table className="w-full text-xs">
        <thead className="text-left text-[var(--color-muted)]">
          <tr>
            <th className="py-1.5 pr-4 font-medium">Revision</th>
            <th className="py-1.5 pr-4 font-medium">Status</th>
            <th className="py-1.5 pr-4 font-medium">Agent</th>
            <th className="py-1.5 pr-4 font-medium">Hash</th>
            <th className="py-1.5 pr-4 font-medium">Author</th>
            <th className="py-1.5 pr-4 font-medium">Approved by</th>
            <th className="py-1.5 font-medium">Created</th>
            <th className="py-1.5" aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {(revisions.data?.revisions ?? []).map((r) => (
            <tr key={r.revision_id} className="border-t border-[var(--color-border)]">
              <td className="py-1.5 pr-4 font-mono">{r.revision_id}</td>
              <td className="py-1.5 pr-4">
                <StatusBadge status={r.status} />
              </td>
              <td className="py-1.5 pr-4 font-mono">{r.agent_id}</td>
              <td className="py-1.5 pr-4 font-mono">{r.config_hash?.slice(0, 12) ?? "—"}</td>
              <td className="py-1.5 pr-4">{r.author ?? "—"}</td>
              <td className="py-1.5 pr-4">{r.approved_by ?? "—"}</td>
              <td className="py-1.5">{formatWhen(r.created_at)}</td>
              <td className="py-1.5 text-right">
                {r.status === "poisoned" && r.proposal_id && (
                  <button
                    type="button"
                    onClick={() => setReactivateId(r.proposal_id)}
                    className="rounded-md border border-[var(--color-border)] px-2 py-0.5 text-[11px]"
                  >
                    Reactivate
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-[11px] text-[var(--color-muted)]">
        Bodies are never listed (D8). Read one agent's config on the Agents page, where the reveal is
        role-gated and audited.
      </p>

      <Modal open={reactivateId !== null} title="Reactivate this proposal?" onClose={() => setReactivateId(null)}>
        <p className="mb-3 text-xs text-[var(--color-muted)]">
          A poisoned revision is re-approved and delivery resumes — validation re-runs first, because the
          reason it failed may have been fixed since. Nothing reactivates on a timer.
        </p>
        <label className="mb-1 block text-xs" htmlFor="reactivate-reason">
          Why is it safe now? (audit log)
        </label>
        <input
          id="reactivate-reason"
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="mb-4 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm"
        />
        {error && <p className="mb-2 text-xs text-[var(--color-danger)]">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={() => setReactivateId(null)} className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs">
            Cancel
          </button>
          <button
            type="button"
            data-testid="confirm-reactivate"
            onClick={reactivate}
            className="rounded-md border border-[var(--color-danger)] px-3 py-1.5 text-xs font-medium text-[var(--color-danger)]"
          >
            Reactivate
          </button>
        </div>
      </Modal>
    </div>
  );
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
