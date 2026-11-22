import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  listAgents,
  getAgentTypes,
  type AgentTypeSummary,
} from "../api/endpoints";
import { getAlerts } from "../api/admin";
import {
  listConfigRevisions,
  listConfigRollouts,
} from "../api/config";
import { AgentTypeBadge } from "../components/AgentTypeBadge";
import { EmptyState } from "../components/EmptyState";
import { StatusBadge } from "../components/StatusBadge";
import { buttonStyles } from "../components/ui/Button";
import { PageHeader } from "../components/ui/PageHeader";

/**
 * Overview (2027-ui Stage B): `/` is the landing page the owner asked for —
 * "a more generic landing page showing people the system" — and the agents
 * table lives at `/agents`. Everything here is derived from existing GET
 * endpoints (no new server surface): the fleet list + query schema for the
 * health/type counts, the config revisions and rollouts for activity, the
 * alerts config for what is armed.
 */

/** Rollout states the strip treats as done (from RolloutMonitor's vocabulary). */
const ROLLOUT_TERMINAL = new Set(["completed", "aborted", "cancelled", "failed"]);

function Stat({
  label,
  value,
  to,
  tone,
}: {
  label: string;
  value: number | string;
  to?: string;
  tone?: "ok" | "danger" | "muted";
}) {
  const toneClass =
    tone === "ok"
      ? "text-[var(--color-ok)]"
      : tone === "danger"
        ? "text-[var(--color-danger)]"
        : tone === "muted"
          ? "text-[var(--color-muted)]"
          : "text-[var(--color-text)]";
  const body = (
    <div className="card flex min-h-24 flex-col justify-between px-4 py-3.5 transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:border-[var(--color-border-strong)]">
      <span className="text-xs font-medium text-[var(--color-muted)]">{label}</span>
      <span className={`font-display text-[28px] font-semibold leading-none tracking-[-0.01em] tnum ${toneClass}`}>
        {value}
      </span>
    </div>
  );
  return to ? (
    <Link to={to} className="focus-visible:outline-none">
      {body}
    </Link>
  ) : (
    body
  );
}

/** One "where next" card — quiet until hovered, then the border speaks. */
function EntryCard({
  to,
  title,
  detail,
}: {
  to: string;
  title: string;
  detail: string;
}) {
  return (
    <Link
      to={to}
      className="card group flex flex-col gap-1 px-4 py-4 no-underline transition-[border-color,background-color] duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:border-[var(--color-border-strong)] hover:bg-[var(--color-surface-2)]/60"
    >
      <span className="flex items-center justify-between text-[15px] font-medium text-[var(--color-text)]">
        {title}
        <span
          aria-hidden
          className="text-[var(--color-faint)] transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover:translate-x-0.5 group-hover:text-[var(--color-accent-strong)]"
        >
          →
        </span>
      </span>
      <span className="text-[13px] leading-relaxed text-[var(--color-muted)]">{detail}</span>
    </Link>
  );
}

/** The health/type counts need the fleet list; the strip needs config state. */
function useOverviewData() {
  const agents = useQuery({
    queryKey: ["agents", {}],
    queryFn: () => listAgents(),
    refetchInterval: 30_000,
  });
  const types = useQuery({
    queryKey: ["agent-types"],
    queryFn: getAgentTypes,
    staleTime: 5 * 60_000,
  });
  const revisions = useQuery({
    queryKey: ["config-revisions", { limit: 100 }],
    queryFn: () => listConfigRevisions({ limit: 100 }),
  });
  const rollouts = useQuery({
    queryKey: ["config-rollouts", { limit: 20 }],
    queryFn: () => listConfigRollouts({ limit: 20 }),
  });
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: getAlerts });
  return { agents, types, revisions, rollouts, alerts };
}

export default function Overview() {
  const { agents, types, revisions, rollouts, alerts } = useOverviewData();

  const fleet = agents.data;
  const total = fleet?.count ?? 0;
  const healthy = fleet?.agents.filter((a) => a.healthy === true).length ?? 0;
  const unhealthy = fleet?.agents.filter((a) => a.healthy === false).length ?? 0;
  const unknownHealth = total - healthy - unhealthy;

  // Type counts with one exemplar per type (for the badge's provenance word).
  const byType = new Map<string, { count: number; source: string }>();
  for (const a of fleet?.agents ?? []) {
    const entry = byType.get(a.agent_type);
    byType.set(a.agent_type, {
      count: (entry?.count ?? 0) + 1,
      source: entry?.source ?? a.agent_type_source,
    });
  }

  // Revisions in "proposed" status are the approval queue (Remote Config).
  const pendingProposals =
    revisions.data?.revisions.filter((r) => r.status === "proposed").length ?? 0;
  const activeRollouts =
    rollouts.data?.rollouts.filter((r) => !ROLLOUT_TERMINAL.has(r.status)).length ?? 0;
  const armedAlerts = alerts.data
    ? Object.values(alerts.data.config.events).filter((e) => e.enabled).length
    : 0;

  const loading =
    agents.isPending || revisions.isPending || rollouts.isPending || alerts.isPending;

  return (
    <section className="flex flex-col gap-4">
      <PageHeader
        title="Overview"
        lede="The fleet at a glance — health, configuration activity, and where to go next."
        meta={fleet ? `${total} agent${total === 1 ? "" : "s"} connected` : undefined}
      />

      {agents.isError && (
        <EmptyState
          title="Could not load the fleet"
          detail={String(agents.error)}
        />
      )}

      {fleet && total === 0 && (
        /* The empty-fleet variant hands off to Stage C/D's quickstart; until
           that lands it points at the connect guide (Help → agent types). */
        <EmptyState
          title="No agents connected yet"
          detail="Point an OpAMP agent at this server and it will appear here. Help lists each agent type's connect snippet and the tokens they need."
        >
          <Link to="/help" className={buttonStyles({ size: "sm" })}>
            Open Help
          </Link>
          <Link to="/agent-tokens" className={buttonStyles({ size: "sm" })}>
            Agent tokens
          </Link>
        </EmptyState>
      )}

      {fleet && total > 0 && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="Fleet health">
            <Stat label="Agents connected" value={total} to="/agents" />
            <Stat label="Healthy" value={healthy} tone="ok" to="/agents" />
            <Stat label="Unhealthy" value={unhealthy} tone="danger" to="/agents" />
            <Stat label="Health unknown" value={unknownHealth} tone="muted" to="/agents" />
          </div>

          {byType.size > 0 && (
            <div className="card px-4 py-3.5">
              <span className="text-xs font-medium text-[var(--color-muted)]">
                Agents by type
              </span>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
                {[...byType.entries()].map(([type, { count, source }]) => (
                  <span key={type} className="flex items-center gap-1.5 text-[13px]">
                    <AgentTypeBadge
                      agentType={type}
                      source={source}
                      profiles={types.data?.types as AgentTypeSummary[]}
                    />
                    <span className="tnum text-[var(--color-muted)]">× {count}</span>
                  </span>
                ))}
              </div>
            </div>
          )}

          <div aria-label="Recent activity">
            <div className="grid gap-3 sm:grid-cols-3">
              <Link
                to="/remote-config"
                className="card flex items-center justify-between px-4 py-3.5 no-underline transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:border-[var(--color-border-strong)]"
              >
                <span className="text-[13px] text-[var(--color-muted)]">
                  Config revisions awaiting approval
                </span>
                <span className="tnum font-display text-xl font-semibold text-[var(--color-text)]">
                  {loading && revisions.isPending ? "…" : pendingProposals}
                </span>
              </Link>
              <Link
                to="/remote-config"
                className="card flex items-center justify-between px-4 py-3.5 no-underline transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:border-[var(--color-border-strong)]"
              >
                <span className="text-[13px] text-[var(--color-muted)]">
                  Rollouts in progress
                </span>
                <span className="tnum font-display text-xl font-semibold text-[var(--color-text)]">
                  {loading && rollouts.isPending ? "…" : activeRollouts}
                </span>
              </Link>
              <Link
                to="/alerts"
                className="card flex items-center justify-between px-4 py-3.5 no-underline transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:border-[var(--color-border-strong)]"
              >
                <span className="text-[13px] text-[var(--color-muted)]">
                  Alert events armed
                </span>
                <span className="tnum font-display text-xl font-semibold text-[var(--color-text)]">
                  {loading && alerts.isPending ? "…" : armedAlerts}
                </span>
              </Link>
            </div>
          </div>

          {fleet.agents.some((a) => a.health_status && a.health_status !== "ok") && (
            <div className="card px-4 py-3.5">
              <span className="text-xs font-medium text-[var(--color-muted)]">
                Agents reporting non-ok health
              </span>
              <div className="mt-2 flex flex-wrap gap-2">
                {fleet.agents
                  .filter((a) => a.health_status && a.health_status !== "ok")
                  .map((a) => (
                    <Link
                      key={a.id}
                      to="/agents/$agentId"
                      params={{ agentId: a.id }}
                      className="flex items-center gap-2 no-underline"
                    >
                      <StatusBadge status={a.health_status} />
                      <span className="text-[13px] text-[var(--color-text)] hover:underline">
                        {a.name ?? a.id}
                      </span>
                    </Link>
                  ))}
              </div>
            </div>
          )}
        </>
      )}

      <div aria-label="Go to">
        <div className="grid gap-3 sm:grid-cols-2">
          <EntryCard
            to="/agents"
            title="Agents"
            detail="Everything connected, selected with read-only SQL over the live fleet."
          />
          <EntryCard
            to="/remote-config"
            title="Remote Config"
            detail="Compose a config, route it through approval and watch the rollout."
          />
          <EntryCard
            to="/reports"
            title="Reports"
            detail="Server-generated fleet analysis — heavy and outdated collectors."
          />
          <EntryCard
            to="/help"
            title="Help"
            detail="Connecting agents, the SQL query language, roles and alerts."
          />
        </div>
      </div>
    </section>
  );
}
