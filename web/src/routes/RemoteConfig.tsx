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
import { PageHeader } from "../components/ui/PageHeader";
import { Tabs } from "../components/ui/Tabs";
import { Button } from "../components/ui/Button";
import { Notice } from "../components/ui/Toast";

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
      <PageHeader
        title="Remote Config"
        lede="Validate → push → canary → gated batches. The push is the approval; nothing reaches the rest of the fleet without evidence that the canary came back healthy."
      />

      {posture.data && <PostureBanner posture={posture.data} />}

      <Tabs
        ariaLabel="Remote config views"
        tabs={TABS}
        active={tab}
        onChange={(id) => setTab(id as Tab)}
        className="self-start"
      />

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
      <Notice kind="error">
        <strong>Config push is blocked by the security posture.</strong> Unmet: {unmet.join(", ")}. Push
        stays refused until every requirement is met.
      </Notice>
    );
  }
  if (posture.insecure) {
    return (
      <Notice kind="warn">
        <strong>Config push is enabled on an insecure posture</strong> (no TLS, agent auth or audit). Do
        not use this on a real fleet.
      </Notice>
    );
  }
  if (!posture.allowed) {
    return (
      <Notice kind="info">
        Config push is <strong>disabled</strong>. Set <code className="font-mono">OPAMP_CONFIG_PUSH_ENABLED=true</code> on a
        server whose posture passes.
      </Notice>
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
      <div className="card overflow-x-auto">
      <table className="data-table">
        <thead>
          <tr>
            <th>Rollout</th>
            <th>Status</th>
            <th>Batch</th>
            <th>Agents</th>
            <th>Paused</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {(rollouts.data?.rollouts ?? []).map((r) => (
            <tr
              key={r.rollout_id}
              className="cursor-pointer"
              onClick={() => setOpenId(openId === r.rollout_id ? null : r.rollout_id)}
            >
              <td className="font-mono text-xs">{r.rollout_id}</td>
              <td>
                <StatusBadge status={r.status} />
              </td>
              <td className="tnum text-xs">
                {Math.min(r.batch.current + 1, r.batch.count)}/{r.batch.count}
              </td>
              <td className="text-xs">{r.agents}</td>
              <td className="text-xs">{r.paused_reason ?? "—"}</td>
              <td className="tnum text-xs">{formatWhen(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
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
      <div className="card overflow-x-auto">
      <table className="data-table">
        <thead>
          <tr>
            <th>Revision</th>
            <th>Status</th>
            <th>Agent</th>
            <th>Hash</th>
            <th>Author</th>
            <th>Approved by</th>
            <th>Created</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {(revisions.data?.revisions ?? []).map((r) => (
            <tr key={r.revision_id}>
              <td className="font-mono text-xs">{r.revision_id}</td>
              <td>
                <StatusBadge status={r.status} />
              </td>
              <td className="font-mono text-xs">{r.agent_id}</td>
              <td className="font-mono text-xs">{r.config_hash?.slice(0, 12) ?? "—"}</td>
              <td className="text-xs">{r.author ?? "—"}</td>
              <td className="text-xs">{r.approved_by ?? "—"}</td>
              <td className="tnum text-xs">{formatWhen(r.created_at)}</td>
              <td className="text-right">
                {r.status === "poisoned" && r.proposal_id && (
                  <button
                    type="button"
                    onClick={() => setReactivateId(r.proposal_id)}
                    className="inline-flex h-7 items-center rounded-[var(--radius-sm)] border border-[var(--color-border)] px-2.5 text-xs font-medium text-[var(--color-muted)] hover:text-[var(--color-text)]"
                  >
                    Reactivate
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <p className="text-xs text-[var(--color-muted)]">
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
          className="field mb-4"
        />
        {error && <p className="mb-2 text-xs text-[var(--color-danger)]">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setReactivateId(null)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            data-testid="confirm-reactivate"
            onClick={reactivate}
          >
            Reactivate
          </Button>
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
