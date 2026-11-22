import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import {
  getAgent,
  getAgentTypes,
  type AgentDetail,
  type AgentTypeSummary,
} from "../api/endpoints";
import { ApiError } from "../api/errors";
import { EmptyState } from "../components/EmptyState";
import { KeyValue, Section } from "../components/Section";
import { StatusBadge } from "../components/StatusBadge";
import { AgentTypeBadge } from "../components/AgentTypeBadge";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Switch } from "../components/ui/Switch";
import { Notice } from "../components/ui/Toast";
import { absoluteWhen, heartbeatAge } from "../lib/heartbeat";
import { useNow } from "../lib/useNow";
import { configObjects } from "../lib/configText";

/**
 * Agent detail (new-ui stage 5): identity/health common to every agent, plus
 * blocks that adapt to the agent's type — the profile's own telemetry panels
 * (`GET /agent/types`), a component inventory only when the profile has the
 * concept, and the profile's guidance. Nothing on this page assumes a
 * collector that the server did not say.
 */

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function formatMetric(value: number, format: string): string {
  switch (format) {
    case "mebibytes":
      return `${(value / 1024 / 1024).toFixed(1)} MiB`;
    case "decimal":
      return value.toFixed(2);
    default:
      return value.toLocaleString();
  }
}

/** The profile's declared telemetry panels, rendered from `GET /agent/types`. */
function TelemetryPanels({
  profile,
  metrics,
}: {
  profile: AgentTypeSummary | undefined;
  metrics: Record<string, number>;
}) {
  const telemetry = profile?.telemetry;
  const panels = profile?.panels ?? [];

  if (!telemetry) {
    // A product fact, not a problem (#122): this profile's agents do not
    // report metrics to this server at all.
    if (Object.keys(metrics).length > 0) {
      return (
        <Section title="Telemetry" caption="All metrics this agent reported:">
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-1 text-xs">
            {Object.entries(metrics).map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="min-w-0 truncate font-mono">{key}</dt>
                <dd className="text-right tabular-nums">{value.toLocaleString()}</dd>
              </div>
            ))}
          </dl>
        </Section>
      );
    }
    return (
      <Section title="Telemetry">
        <p className="text-xs text-[var(--color-muted)]">
          This profile does not report telemetry to the server.
        </p>
      </Section>
    );
  }

  if (Object.keys(metrics).length === 0) {
    return (
      <Section title={telemetry.title} caption={telemetry.caption}>
        <p className="text-xs text-[var(--color-muted)]">No telemetry received yet.</p>
      </Section>
    );
  }

  return (
    <Section title={telemetry.title} caption={telemetry.caption}>
      <div className="flex flex-col gap-4">
        {panels.map((panel) => {
          if (panel.widget === "alert") {
            const value = panel.metric ? metrics[panel.metric] : undefined;
            if (panel.when === "above_zero" && value && value > 0) {
              const severity: "error" | "warn" = panel.severity === "error" ? "error" : "warn";
              return (
                <Notice key={panel.id} kind={severity}>
                  {(panel.message ?? "⚠️ {value}").replace("{value}", value.toLocaleString())}
                </Notice>
              );
            }
            return null;
          }
          if (panel.widget !== "metrics") return null;
          return (
            <div key={panel.id}>
              {panel.title && <h3 className="text-sm font-medium">{panel.title}</h3>}
              {panel.caption && (
                <p className="mt-0.5 mb-2 text-xs text-[var(--color-muted)]">{panel.caption}</p>
              )}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {(panel.metrics ?? []).map((m) => (
                  <div key={m.metric} className="card-2 p-3">
                    <div className="text-xs text-[var(--color-faint)]">{m.label}</div>
                    <div className="tnum mt-1 font-display text-xl font-semibold tracking-[-0.01em]">
                      {metrics[m.metric] === undefined
                        ? "—"
                        : formatMetric(metrics[m.metric], m.format)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </Section>
  );
}

/** Component inventory — a profile concept; rendered only when the profile has it. */
function ComponentsSection({ profile, agent }: { profile?: AgentTypeSummary; agent: AgentDetail }) {
  if (!profile?.has_components) return null;

  // #120: `components_status` alone cannot say "no components" from "not told
  // yet", and both used to render as "0".
  if (agent.components_status === "none") {
    return (
      <Section title="Components">
        <p className="text-xs text-[var(--color-muted)]">
          This product has no component inventory.
        </p>
      </Section>
    );
  }

  const comps = agent.components ?? {};
  const rows = Object.entries(comps).flatMap(([section, list]) =>
    (list ?? []).map((c) => ({ ...c, section })),
  );
  if (rows.length === 0) {
    return (
      <Section title="Components">
        <p className="text-xs text-[var(--color-muted)]">No components reported yet.</p>
      </Section>
    );
  }

  return <ComponentsTable rows={rows} />;
}

function ComponentsTable({
  rows,
}: {
  rows: { id: string; used?: boolean; version?: string; section: string }[];
}) {
  const [inUseOnly, setInUseOnly] = useState(false);
  const shown = inUseOnly ? rows.filter((r) => r.used) : rows;
  return (
    <Section
      title="Components"
      actions={
        <Switch
          label="In use only"
          checked={inUseOnly}
          onChange={setInUseOnly}
        />
      }
    >
      <table className="data-table">
        <thead>
          <tr>
            <th>Type</th>
            <th>Name</th>
            <th>Version</th>
            <th>In use</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={`${r.section}/${r.id}`}>
              <td className="text-xs">{r.section}</td>
              <td className="font-mono text-xs">{r.id}</td>
              <td className="text-xs text-[var(--color-muted)]">{r.version ?? "—"}</td>
              <td>
                <StatusBadge
                  tone={r.used ? "ok" : "muted"}
                  label={r.used ? "in use" : "unused"}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

/** Effective config: display-only. Proposal/editing is stage 6. */
function ConfigSection({ agent }: { agent: AgentDetail }) {
  const objects = configObjects(agent.effective_config);
  return (
    <Section
      title="Effective config"
      caption={
        agent.effective_config_redacted
          ? "The server masked values it considers secret (listed below). A masked value means “keep what is stored” — it is opaque."
          : undefined
      }
    >
      {agent.config_error ? (
        <Notice kind="warn" className="mb-2">
          {String(agent.config_error)}
        </Notice>
      ) : null}
      {objects.length === 0 ? (
        <p className="text-xs text-[var(--color-muted)]">
          No config object in this agent's report is readable as YAML or JSON.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {objects.map((obj) => (
            <div key={obj.key}>
              <div className="mb-1 flex items-baseline gap-2 text-[11px] text-[var(--color-muted)]">
                <span className="font-mono">object {obj.key || "<empty key>"}</span>
                {obj.contentType && <span>· {obj.contentType}</span>}
              </div>
              <pre className="card-2 max-h-96 overflow-auto bg-[var(--color-bg)] p-3 text-xs leading-relaxed">
                {obj.body}
              </pre>
            </div>
          ))}
        </div>
      )}
      {agent.redacted_paths.length > 0 && (
        <p className="mt-2 text-[11px] text-[var(--color-muted)]">
          Masked paths: <code className="font-mono">{agent.redacted_paths.join(", ")}</code>
        </p>
      )}
    </Section>
  );
}

export default function AgentDetail() {
  // `strict: false`: the component sits under the shell layout route, so the
  // root-relative match id does not resolve here (same as useSearch above).
  const { agentId } = useParams({ strict: false }) as { agentId: string };
  // Relative "last heartbeat" ages tick while the page is open.
  const now = useNow();
  const search = useSearch({ strict: false }) as { q?: string } | undefined;
  const carriedQuery = search?.q ? { q: search.q } : {};
  const agent = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => getAgent(agentId),
    retry: false,
  });
  const types = useQuery({
    queryKey: ["agent-types"],
    queryFn: getAgentTypes,
    staleTime: 5 * 60_000,
  });

  if (agent.isPending) {
    return <p className="text-sm text-[var(--color-muted)]">Loading agent…</p>;
  }

  if (agent.isError) {
    const notFound = agent.error instanceof ApiError && agent.error.status === 404;
    return (
      <EmptyState
        title={notFound ? "Agent not found" : "Could not load agent"}
        detail={
          notFound
            ? "This agent is no longer connected. It may have disconnected or restarted under a new id."
            : agent.error instanceof ApiError
              ? agent.error.message
              : String(agent.error)
        }
      >
        <Link to="/agents" search={carriedQuery} className="text-[13px] text-[var(--color-accent)] hover:underline">
          ← All agents
        </Link>
      </EmptyState>
    );
  }

  const data = agent.data;
  const profile = types.data?.types.find((t) => t.name === data.agent_type);
  const metrics = data.metrics?.metrics ?? {};
  const healthTone = data.healthy === true ? "ok" : data.healthy === false ? "danger" : "muted";
  const healthLabel =
    data.healthy === true ? "Healthy" : data.healthy === false ? "Unhealthy" : "Unknown";

  const attrs = (() => {
    const doc = data.description as {
      identifyingAttributes?: { key: string; value?: Record<string, unknown> }[];
      nonIdentifyingAttributes?: { key: string; value?: Record<string, unknown> }[];
    } | null;
    if (!doc) return [] as [string, string][];
    return [
      ...(doc.identifyingAttributes ?? []),
      ...(doc.nonIdentifyingAttributes ?? []),
    ].map((a) => {
      const first = a.value ? Object.values(a.value)[0] : undefined;
      return [a.key, first === undefined ? "—" : String(first)] as [string, string];
    });
  })();

  const typeWarningSource = ["declared", "pinned"].includes(data.agent_type_source);

  return (
    <section className="flex flex-col gap-4">
      <PageHeader
        title={data.name ?? "Agent details"}
        lede={data.id}
        actions={
          <Link to="/agents" search={carriedQuery}>
            <Button size="sm">← All agents</Button>
          </Link>
        }
      />

      {data.uid_reissued_from && (
        <Notice kind="warn">
          This agent's id changed: it previously connected as{" "}
          <code className="font-mono">{data.uid_reissued_from}</code>
          {data.uid_reissue_reason ? ` (${data.uid_reissue_reason})` : ""}. The server renamed
          it because the old uid was re-issued to another agent.
        </Notice>
      )}

      <div className="card grid grid-cols-2 gap-4 p-4 sm:grid-cols-3 lg:grid-cols-6">
        <KeyValue label="Status">
          <StatusBadge tone={healthTone} label={healthLabel} />
          {data.health_error && (
            <div className="mt-1 text-[11px] text-[var(--color-danger)]">{data.health_error}</div>
          )}
        </KeyValue>
        <KeyValue label="Type">
          <AgentTypeBadge
            agentType={data.agent_type}
            source={data.agent_type_source}
            profiles={types.data?.types}
          />
        </KeyValue>
        <KeyValue label="Remote config">
          <StatusBadge status={data.remote_config_status} />
        </KeyValue>
        <KeyValue label="Compliance">
          {(() => {
            const c = data.compliance;
            const label =
              c?.opa_disabled
                ? "OPA off"
                : c?.compliant === true
                  ? "Compliant"
                  : c?.compliant === false
                    ? "Non-compliant"
                    : "Unknown";
            const tone =
              c?.compliant === true ? "ok" : c?.compliant === false ? "danger" : "muted";
            return <StatusBadge tone={tone} label={label} />;
          })()}
        </KeyValue>
        <KeyValue label="Credential">
          {data.credential.enrolled
            ? data.credential.active > 0
              ? "Enrolled"
              : "Revoked"
            : "Not enrolled"}
        </KeyValue>
        <KeyValue label="Last heartbeat">
          <span title={absoluteWhen(data.last_heartbeat)}>
            {heartbeatAge(data.last_heartbeat, now)}
          </span>
          <div className="text-[11px] text-[var(--color-muted)]">
            every {data.report_interval_seconds?.toFixed(1) ?? "?"}s · stale after{" "}
            {data.stale_timeout_seconds}s
          </div>
        </KeyValue>
      </div>

      {data.agent_type_warning && (
        <Notice kind={typeWarningSource ? "info" : "warn"}>
          {data.agent_type_warning}
        </Notice>
      )}
      {data.agent_type_ambiguous && data.agent_type_ambiguous.length > 0 && (
        <Notice kind="info">
          This agent also matches: {data.agent_type_ambiguous.join(", ")}. Match priority chose
          this one; pin the type to settle it.
        </Notice>
      )}

      {profile && (
        <Section
          title={`About ${profile.display_name}`}
          caption="From this agent type's profile on the server."
        >
          <p className="max-w-prose text-sm">{profile.summary}</p>
          {profile.guidance && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-[var(--color-muted)] hover:text-[var(--color-text)]">
                Type guidance
              </summary>
              <p className="mt-1 max-w-prose whitespace-pre-wrap text-xs text-[var(--color-muted)]">
                {profile.guidance}
              </p>
            </details>
          )}
        </Section>
      )}

      <TelemetryPanels profile={profile} metrics={metrics} />

      <ComponentsSection profile={profile} agent={data} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Remote config & delivery">
          <dl className="grid gap-3 sm:grid-cols-2">
            <KeyValue label="Status">
              <StatusBadge status={data.remote_config_status} />
            </KeyValue>
            {data.remote_config_error && (
              <KeyValue label="Error">
                <span className="text-[var(--color-danger)]">{data.remote_config_error}</span>
              </KeyValue>
            )}
            <KeyValue label="Pending command">
              {data.pending_command ? (
                <>
                  {data.pending_command}
                  <div className="text-[11px] text-[var(--color-muted)]">
                    {data.command_delivered_at
                      ? `delivered ${formatWhen(data.command_delivered_at)}`
                      : "queued"}
                  </div>
                </>
              ) : (
                "None"
              )}
            </KeyValue>
            <KeyValue label="Config requests ignored">
              {data.components_request_ignored}
            </KeyValue>
          </dl>
        </Section>

        <Section title="Capabilities">
          <ul className="flex flex-wrap gap-1.5">
            {data.capability_tags.length === 0 && (
              <li className="text-xs text-[var(--color-muted)]">No capabilities</li>
            )}
            {data.capability_tags.map((tag) => (
              <li
                key={tag.label}
                className="rounded-full border border-[var(--color-border)] bg-[var(--color-surface-2)] px-2.5 py-0.5 text-xs"
              >
                <span aria-hidden className="mr-1">{tag.icon}</span>
                {tag.label}
              </li>
            ))}
          </ul>
          {data.custom_capabilities.length > 0 && (
            <>
              <h3 className="mt-3 text-xs text-[var(--color-faint)]">
                Custom capabilities
              </h3>
              <ul className="mt-1 flex flex-wrap gap-1.5">
                {data.custom_capabilities.map((c) => (
                  <li key={c} className="rounded-full border border-[var(--color-border)] bg-[var(--color-surface-2)] px-2.5 py-0.5 font-mono text-xs">
                    {c}
                  </li>
                ))}
              </ul>
              {data.pending_custom_message && (
                <p className="mt-2 text-[11px] text-[var(--color-muted)]">
                  Queued: <code className="font-mono">{data.pending_custom_message.capability}</code> /{" "}
                  <code className="font-mono">{data.pending_custom_message.type}</code> (waiting for the agent's next poll)
                </p>
              )}
            </>
          )}
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Description attributes">
          {attrs.length === 0 ? (
            <p className="text-xs text-[var(--color-muted)]">No attributes</p>
          ) : (
            <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-4 gap-y-1 text-xs">
              {attrs.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="min-w-0 truncate font-mono text-[var(--color-muted)]">{key}</dt>
                  <dd className="min-w-0 break-words">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </Section>

        <Section title="Credential">
          <dl className="grid gap-3 sm:grid-cols-2">
            <KeyValue label="Enrolled">{data.credential.enrolled ? "Yes" : "No"}</KeyValue>
            <KeyValue label="Active credentials">{data.credential.active}</KeyValue>
            <KeyValue label="Last seen">{formatWhen(data.credential.last_seen_at)}</KeyValue>
            {data.credential.revoked_at && (
              <KeyValue label="Revoked at">
                <span className="text-[var(--color-danger)]">{formatWhen(data.credential.revoked_at)}</span>
              </KeyValue>
            )}
          </dl>
          <p className="mt-2 text-[11px] text-[var(--color-muted)]">
            Tokens are never shown — only their hash is stored server-side.
          </p>
        </Section>
      </div>

      <ConfigSection agent={data} />
    </section>
  );
}
