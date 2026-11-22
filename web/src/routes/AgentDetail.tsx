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
              const severity = panel.severity === "error" ? "danger" : "warn";
              return (
                <p
                  key={panel.id}
                  role="status"
                  className={
                    "rounded-md border px-3 py-2 text-sm " +
                    (severity === "danger"
                      ? "border-[var(--color-danger)] text-[var(--color-danger)]"
                      : "border-[var(--color-warn)] text-[var(--color-warn)]")
                  }
                >
                  {(panel.message ?? "⚠️ {value}").replace("{value}", value.toLocaleString())}
                </p>
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
                  <div key={m.metric} className="rounded-md border border-[var(--color-border)] p-3">
                    <div className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                      {m.label}
                    </div>
                    <div className="mt-1 text-lg tabular-nums">
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
        <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-muted)]">
          <input
            type="checkbox"
            checked={inUseOnly}
            onChange={(e) => setInUseOnly(e.target.checked)}
          />
          In use only
        </label>
      }
    >
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="border-b border-[var(--color-border)] uppercase tracking-wide text-[var(--color-muted)]">
            <th scope="col" className="py-1.5 pr-3 font-medium">Type</th>
            <th scope="col" className="py-1.5 pr-3 font-medium">Name</th>
            <th scope="col" className="py-1.5 pr-3 font-medium">Version</th>
            <th scope="col" className="py-1.5 font-medium">In use</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={`${r.section}/${r.id}`} className="border-b border-[var(--color-border)] last:border-0">
              <td className="py-1.5 pr-3">{r.section}</td>
              <td className="py-1.5 pr-3 font-mono">{r.id}</td>
              <td className="py-1.5 pr-3 text-[var(--color-muted)]">{r.version ?? "—"}</td>
              <td className="py-1.5">
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
        <p className="mb-2 rounded-md border border-[var(--color-warn)] px-3 py-2 text-xs text-[var(--color-warn)]">
          {String(agent.config_error)}
        </p>
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
              <pre className="max-h-96 overflow-auto rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-3 font-mono text-xs leading-relaxed">
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
        <Link to="/" search={carriedQuery} className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs hover:text-[var(--color-text)]">
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
      <div>
        <Link
          to="/"
          search={carriedQuery}
          className="text-xs text-[var(--color-muted)] hover:text-[var(--color-text)]"
        >
          ← All agents
        </Link>
        <h1 className="mt-1 text-lg font-semibold">
          {data.name ?? <span className="text-[var(--color-muted)]">Agent details</span>}
        </h1>
        <p className="mt-0.5 font-mono text-[11px] text-[var(--color-muted)]">{data.id}</p>
      </div>

      {data.uid_reissued_from && (
        <p
          role="status"
          className="rounded-md border border-[var(--color-warn)] px-3 py-2 text-sm text-[var(--color-warn)]"
        >
          This agent's id changed: it previously connected as{" "}
          <code className="font-mono">{data.uid_reissued_from}</code>
          {data.uid_reissue_reason ? ` (${data.uid_reissue_reason})` : ""}. The server renamed
          it because the old uid was re-issued to another agent.
        </p>
      )}

      <div className="grid grid-cols-2 gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 sm:grid-cols-3 lg:grid-cols-6">
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
          {formatWhen(data.last_heartbeat)}
          <div className="text-[11px] text-[var(--color-muted)]">
            every {data.report_interval_seconds?.toFixed(1) ?? "?"}s · stale after{" "}
            {data.stale_timeout_seconds}s
          </div>
        </KeyValue>
      </div>

      {data.agent_type_warning && (
        <p
          role="status"
          className={
            "rounded-md border px-3 py-2 text-xs " +
            (typeWarningSource
              ? "border-[var(--color-border)] text-[var(--color-muted)]"
              : "border-[var(--color-warn)] text-[var(--color-warn)]")
          }
        >
          {data.agent_type_warning}
        </p>
      )}
      {data.agent_type_ambiguous && data.agent_type_ambiguous.length > 0 && (
        <p className="rounded-md border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-muted)]">
          This agent also matches: {data.agent_type_ambiguous.join(", ")}. Match priority chose
          this one; pin the type to settle it.
        </p>
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
                className="rounded-md border border-[var(--color-border)] px-2 py-0.5 text-xs"
              >
                <span aria-hidden className="mr-1">{tag.icon}</span>
                {tag.label}
              </li>
            ))}
          </ul>
          {data.custom_capabilities.length > 0 && (
            <>
              <h3 className="mt-3 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                Custom capabilities
              </h3>
              <ul className="mt-1 flex flex-wrap gap-1.5">
                {data.custom_capabilities.map((c) => (
                  <li key={c} className="rounded-md border border-[var(--color-border)] px-2 py-0.5 font-mono text-xs">
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
