import { useQuery } from "@tanstack/react-query";
import { listAgents, type AgentEntry } from "../api/endpoints";
import { ApiError } from "../api/errors";

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function StatusDot({ on, label }: { on: boolean | null; label: string }) {
  const tone = on === true ? "ok" : on === false ? "bad" : "muted";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span
        aria-hidden
        className={`h-2 w-2 rounded-full ${
          tone === "ok"
            ? "bg-[var(--color-ok)]"
            : tone === "bad"
              ? "bg-[var(--color-danger)]"
              : "bg-[var(--color-muted)]"
        }`}
      />
      {label}
    </span>
  );
}

function AgentRow({ agent }: { agent: AgentEntry }) {
  return (
    <tr className="border-b border-[var(--color-border)] last:border-0">
      <td className="px-3 py-2">
        <div className="font-medium">{agent.name ?? <em className="text-[var(--color-muted)]">unnamed</em>}</div>
        <div className="font-mono text-[11px] text-[var(--color-muted)]">{agent.id}</div>
      </td>
      <td className="px-3 py-2 text-xs">
        <div>{agent.agent_type}</div>
        <div className="text-[var(--color-muted)]">{agent.agent_type_source}</div>
      </td>
      <td className="px-3 py-2">
        <StatusDot on={agent.healthy} label={agent.health_status ?? (agent.healthy ? "healthy" : "unhealthy")} />
        {agent.health_error && (
          <div className="text-[11px] text-[var(--color-danger)]">{agent.health_error}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {agent.remote_config_status ?? "UNSET"}
        {agent.remote_config_error && (
          <div className="text-[11px] text-[var(--color-danger)]">{agent.remote_config_error}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {agent.credential.enrolled ? (
          <>
            {agent.credential.active > 0 ? "enrolled" : "revoked"}
            <div className="text-[var(--color-muted)]">seen {formatWhen(agent.credential.last_seen_at)}</div>
          </>
        ) : (
          <span className="text-[var(--color-muted)]">not enrolled</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-[var(--color-muted)]">{formatWhen(agent.last_heartbeat)}</td>
    </tr>
  );
}

export default function Agents() {
  const { data, error, isPending, isError, refetch } = useQuery({
    queryKey: ["agents"],
    queryFn: () => listAgents(),
  });

  return (
    <section>
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Agents</h1>
        {data && <span className="text-xs text-[var(--color-muted)]">{data.count} connected</span>}
      </div>

      {isPending && <p className="mt-6 text-sm text-[var(--color-muted)]">Loading agents…</p>}

      {isError && (
        <div className="mt-6 rounded-md border border-[var(--color-danger)] bg-[var(--color-surface)] p-4 text-sm">
          <p className="font-medium">Could not load agents</p>
          <p className="mt-1 text-[var(--color-muted)]">
            {error instanceof ApiError ? error.message : String(error)}
          </p>
          <button onClick={() => refetch()} className="mt-3 rounded border border-[var(--color-border)] px-2 py-1 text-xs">
            Retry
          </button>
        </div>
      )}

      {data && data.agents.length === 0 && (
        <div className="mt-6 rounded-md border border-[var(--color-border)] p-6 text-sm text-[var(--color-muted)]">
          No agents are connected. Point an OpAMP agent at this server and it will appear here.
        </div>
      )}

      {data && data.agents.length > 0 && (
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)] text-xs uppercase tracking-wide text-[var(--color-muted)]">
              <th className="px-3 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Health</th>
              <th className="px-3 py-2 font-medium">Remote config</th>
              <th className="px-3 py-2 font-medium">Credential</th>
              <th className="px-3 py-2 font-medium">Last heartbeat</th>
            </tr>
          </thead>
          <tbody>
            {data.agents.map((agent) => (
              <AgentRow key={agent.id} agent={agent} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
