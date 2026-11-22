import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import {
  listAgents,
  getQuerySchema,
  getAgentTypes,
  attrValue,
  type AgentEntry,
  type AgentTypeSummary,
  type QuerySchemaColumn,
} from "../api/endpoints";
import { ApiError } from "../api/errors";
import { EmptyState } from "../components/EmptyState";
import { SqlSearch } from "../components/SqlSearch";
import { StatusBadge } from "../components/StatusBadge";
import { AgentTypeBadge } from "../components/AgentTypeBadge";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Checkbox } from "../components/ui/Checkbox";

/**
 * Agents list (new-ui stage 5). Selection is server-side only (#132): the SQL
 * search drives `GET /agents?q=...`, the quick health chips use the `healthy`
 * parameter, and nothing is ever filtered in the browser. Columns come from
 * `GET /agents/query/schema` — core columns plus the attributes this fleet
 * actually reports — never from hard-coded collector fields.
 */

/** Columns shown until the operator picks others. All are schema core columns. */
const DEFAULT_COLUMNS = ["agent_type", "healthy", "remote_config_status", "last_heartbeat"];

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Render one schema column's cell for one agent. Unknown columns fall back raw. */
function SchemaCell({
  column,
  agent,
  profiles,
}: {
  column: QuerySchemaColumn;
  agent: AgentEntry;
  profiles: AgentTypeSummary[] | undefined;
}) {
  switch (column.name) {
    case "name":
    case "agent_name":
      return <>{agent.name ?? "—"}</>;
    case "healthy": {
      const tone = agent.healthy === true ? "ok" : agent.healthy === false ? "danger" : "muted";
      const label =
        agent.healthy === true ? "Healthy" : agent.healthy === false ? "Unhealthy" : "Unknown";
      return <StatusBadge tone={tone} label={label} />;
    }
    case "health_status":
      return <StatusBadge status={agent.health_status} />;
    case "remote_config_status":
    case "status":
      return <StatusBadge status={agent.remote_config_status} />;
    case "agent_type":
    case "type":
      return (
        <AgentTypeBadge agentType={agent.agent_type} source={agent.agent_type_source} profiles={profiles} />
      );
    case "compliant":
    case "is_compliant": {
      const c = agent.compliance;
      const label = c?.opa_disabled ? "OPA off" : c?.compliant === true ? "Compliant" : c?.compliant === false ? "Non-compliant" : "Unknown";
      const tone = c?.compliant === true ? "ok" : c?.compliant === false ? "danger" : "muted";
      return <StatusBadge tone={tone} label={label} />;
    }
    case "last_heartbeat":
      return <>{formatWhen(agent.last_heartbeat)}</>;
    case "connected_at":
      return <>{formatWhen(agent.connected_at)}</>;
    case "capabilities":
      return <>{agent.capabilities}</>;
    case "capability_tags":
      return <>{agent.capability_tags.length}</>;
    case "service_name":
    case "service.name":
      return <>{attrValue(agent.description, "service.name") ?? "—"}</>;
    case "service_version":
    case "service.version":
      return <>{attrValue(agent.description, "service.version") ?? "—"}</>;
    default: {
      const value = attrValue(agent.description, column.name);
      return <>{value ?? "—"}</>;
    }
  }
}

/** `GET /agent/types` — profile display names/icons for the type badges. */
function useAgentTypes() {
  return useQuery({ queryKey: ["agent-types"], queryFn: getAgentTypes, staleTime: 5 * 60_000 });
}

function useAgents(q: string | undefined, healthy: "true" | "false" | undefined) {
  return useQuery({
    queryKey: ["agents", { q, healthy }],
    queryFn: () => listAgents({ q, healthy }),
  });
}

function HealthChips({
  value,
  onChange,
  disabled,
}: {
  value: "true" | "false" | undefined;
  onChange: (v: "true" | "false" | undefined) => void;
  disabled: boolean;
}) {
  const chips: { label: string; v: "true" | "false" | undefined }[] = [
    { label: "All", v: undefined },
    { label: "Healthy", v: "true" },
    { label: "Unhealthy", v: "false" },
  ];
  return (
    <div role="group" aria-label="Filter by health" className="flex items-center gap-1">
      {chips.map(({ label, v }) => {
        const active = value === v;
        return (
          <button
            key={label}
            type="button"
            disabled={disabled}
            aria-pressed={active}
            onClick={() => onChange(v)}
            className={
              "inline-flex h-7 items-center rounded-[var(--radius-sm)] border px-2.5 text-xs font-medium transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] " +
              (active
                ? "border-[var(--color-accent)]/50 bg-[var(--color-accent-soft)] text-[var(--color-accent-strong)]"
                : "border-[var(--color-border)] text-[var(--color-muted)]") +
              (disabled ? " opacity-50" : " hover:text-[var(--color-text)]")
            }
            title={disabled ? "A SQL query is the selector; the server ignores other filters while it runs" : undefined}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

function ColumnPicker({
  columns,
  selected,
  onChange,
}: {
  columns: QuerySchemaColumn[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <details className="relative">
      <summary className="inline-flex h-7 cursor-pointer list-none items-center rounded-[var(--radius-sm)] border border-[var(--color-border)] px-2.5 text-xs font-medium text-[var(--color-muted)] transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:text-[var(--color-text)]">
        Columns ({selected.length})
      </summary>
      <ul className="card absolute right-0 z-10 mt-1 max-h-72 w-64 overflow-y-auto p-2 text-xs shadow-[var(--shadow-overlay)]">
        {columns.map((c) => (
          <li key={c.name} className="rounded-[var(--radius-sm)] px-1 hover:bg-[var(--color-surface-2)]">
            <Checkbox
              className="w-full items-start py-1"
              label={
                <span className="min-w-0">
                  <span className="font-mono">{c.sql_name}</span>
                  <span className="ml-1.5 text-[10px] text-[var(--color-faint)]">
                    {c.kind}
                  </span>
                </span>
              }
              checked={selected.includes(c.name)}
              onChange={(next) =>
                onChange(
                  next
                    ? [...selected, c.name]
                    : selected.filter((n) => n !== c.name),
                )
              }
            />
          </li>
        ))}
      </ul>
    </details>
  );
}

export default function Agents() {
  const navigate = useNavigate();
  // The route's validated search (see router.tsx): the query lives in the URL,
  // so a detail page's back link re-enters the same selection (#146 parity).
  // The route's validated search (see router.tsx). `strict: false` because the
  // component sits under the shell layout route, not at the root match itself.
  const search = useSearch({ strict: false }) as { q?: string } | undefined;
  const activeQuery = search?.q ?? "";
  const [healthy, setHealthy] = useState<"true" | "false" | undefined>(undefined);
  const [selectedColumns, setSelectedColumns] = useState<string[]>(DEFAULT_COLUMNS);

  const schema = useQuery({ queryKey: ["agents-query-schema"], queryFn: getQuerySchema });
  const types = useAgentTypes();
  const agents = useAgents(activeQuery || undefined, activeQuery ? undefined : healthy);

  const columns = useMemo(() => {
    const all = schema.data?.columns ?? [];
    return all.filter((c) => c.kind !== "core" || !["id", "agent_id", "attributes", "stale"].includes(c.name));
  }, [schema.data]);

  const selectedSchema = useMemo(
    () => columns.filter((c) => selectedColumns.includes(c.name)),
    [columns, selectedColumns],
  );

  function runQuery(text: string) {
    // One selector at a time (the server refuses q + group together): a new
    // search clears any saved-group filter by simply not carrying it.
    navigate({ to: "/", search: text ? { q: text } : {} });
  }

  const data = agents.data;
  const total = data?.count ?? 0;
  const filtered = activeQuery || healthy;

  return (
    <section className="flex flex-col gap-4">
      <PageHeader
        title="Agents"
        lede="Everything connected to this server, selected with read-only SQL over the live fleet."
        meta={data ? `${total} connected` : undefined}
      />

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0 flex-1 md:max-w-2xl">
          <SqlSearch
            columns={columns}
            value={activeQuery}
            onSubmit={runQuery}
          />
        </div>
        <div className="flex items-center gap-2">
          <HealthChips
            value={healthy}
            onChange={setHealthy}
            disabled={!!activeQuery}
          />
          {columns.length > 0 && (
            <ColumnPicker columns={columns} selected={selectedColumns} onChange={setSelectedColumns} />
          )}
        </div>
      </div>

      {filtered && (
        <p className="text-xs text-[var(--color-muted)]">
          {activeQuery
            ? <>Selection: <code className="font-mono text-[var(--color-accent-strong)]">{activeQuery}</code></>
            : "Filtering by health (server-side)."}
        </p>
      )}

      {agents.isPending && <p className="mt-2 text-sm text-[var(--color-muted)]">Loading agents…</p>}

      {agents.isError && (
        <EmptyState
          title={agents.error instanceof ApiError && agents.error.kind === "client" ? "Query refused" : "Could not load agents"}
          detail={
            agents.error instanceof ApiError
              ? agents.error.message
              : String(agents.error)
          }
        >
          <Button size="sm" onClick={() => agents.refetch()}>
            Retry
          </Button>
        </EmptyState>
      )}

      {data && data.agents.length === 0 && (
        <EmptyState
          title="No agents matched"
          detail={
            activeQuery
              ? "Check the attribute names in the suggestions — an unknown attribute matches nothing rather than failing the query."
              : "No agents are connected. Point an OpAMP agent at this server and it will appear here."
          }
        />
      )}

      {data && data.agents.length > 0 && (
        <div className="card overflow-x-auto">
          <table className="data-table min-w-[48rem]">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                {selectedSchema.map((c) => (
                  <th scope="col" key={c.name}>
                    {c.sql_name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.agents.map((agent) => (
                <tr key={agent.id}>
                  <td>
                    <Link
                      to="/agents/$agentId"
                      params={{ agentId: agent.id }}
                      search={activeQuery ? { q: activeQuery } : {}}
                      className="font-medium text-[var(--color-text)] hover:text-[var(--color-accent-strong)] hover:underline"
                      title={agent.id}
                    >
                      {agent.name ?? <em className="text-[var(--color-muted)]">unnamed</em>}
                    </Link>
                    <div className="mt-0.5 font-mono text-[11px] text-[var(--color-faint)]">{agent.id}</div>
                  </td>
                  {selectedSchema.map((c) => (
                    <td key={c.name} className="text-xs">
                      <SchemaCell column={c} agent={agent} profiles={types.data?.types} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
