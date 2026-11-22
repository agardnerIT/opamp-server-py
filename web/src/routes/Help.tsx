import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { getAgentTypes, type AgentTypeSummary } from "../api/endpoints";

/**
 * Help (new-ui stage 7). The Streamlit page embedded per-profile connect
 * snippets from the Python profile dictionary; the REST API does not expose
 * snippets, so this page says the two universal things (endpoint, credential)
 * and describes each profile from `GET /agent/types` — the same data the
 * detail pages render, never hard-coded collector YAML (#121).
 */

function H2({ children }: { children: string }) {
  return (
    <h2 className="mt-10 border-b border-[var(--color-border)] pb-2 text-base font-semibold first:mt-6">
      {children}
    </h2>
  );
}

function TypeDeclares({ profile }: { profile: AgentTypeSummary }) {
  const declares = [
    `Configuration: ${profile.content_types.length > 0 ? profile.content_types.join(", ") : "any content type, opaque"}`,
    `Structural validation: ${profile.validates_structure ? "yes, judged by its own rules" : "no, the body is not interpreted"}`,
    `Component inventory: ${profile.has_components ? "yes" : "no concept of one"}`,
    `Telemetry view: ${profile.telemetry?.title ?? "this agent reports none here"}`,
    `Reports: ${profile.reports.length > 0 ? profile.reports.join(", ") : "none"}`,
  ];
  return (
    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-[var(--color-muted)]">
      {declares.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  );
}

export default function Help() {
  const types = useQuery({ queryKey: ["agent-types"], queryFn: getAgentTypes, staleTime: 5 * 60_000 });

  return (
    <section className="flex flex-col gap-1 pb-8">
      <h1 className="text-xl font-semibold tracking-[-0.01em]">Help</h1>

      <H2>Connecting an agent</H2>
      <p className="max-w-prose text-sm">
        Every agent needs two things from this server: the <strong>OpAMP endpoint</strong> —
        <code className="mx-1 font-mono text-xs">ws(s)://&lt;this-server&gt;/v1/opamp</code> — and, when the
        server requires one, an <strong>agent token</strong>. Everything else in its configuration
        belongs to the agent's product: this server stores that config as opaque content unless a
        profile in its dictionary says it can interpret it.
      </p>
      <p className="max-w-prose text-sm text-[var(--color-muted)]">
        The per-product config shape is documented per profile in{" "}
        <code className="font-mono text-xs">docs/agent-profiles.md</code>, and a minted token always
        returns a paste-ready snippet in that agent type's own shape.
      </p>

      <H2>Known agent types</H2>
      <p className="max-w-prose text-sm">
        The server's <strong>profile dictionary</strong> decides what it is willing to claim about an
        agent. The same list is <code className="mx-1 font-mono text-xs">GET /agent/types</code>,
        <code className="mx-1 font-mono text-xs">opampctl agents types</code> and the MCP{" "}
        <code className="font-mono text-xs">list_agent_types</code> tool — adding a type is a YAML file
        and a restart, with no server release.
      </p>
      <div className="flex flex-col gap-2">
        {(types.data?.types ?? []).map((profile) => (
          <details key={profile.name} className="card p-3">
            <summary className="cursor-pointer text-sm font-medium">
              <span className="mr-1.5">{profile.icon}</span>
              {profile.display_name}{" "}
              <code className="ml-1 font-mono text-xs text-[var(--color-muted)]">{profile.name}</code>
              {profile.bootstrap_default && (
                <span className="ml-2 rounded-full bg-[var(--color-accent-soft)] px-2 py-0.5 text-[11px] font-medium text-[var(--color-accent-strong)]">
                  bootstrap default
                </span>
              )}
            </summary>
            <p className="mt-1.5 max-w-prose text-xs">{profile.summary}</p>
            <TypeDeclares profile={profile} />
            {profile.guidance && (
              <p className="mt-1.5 max-w-prose text-xs text-[var(--color-warn)]">{profile.guidance}</p>
            )}
          </details>
        ))}
      </div>

      <H2>Agent tokens</H2>
      <p className="max-w-prose text-sm">
        Agents authenticate with <code className="font-mono text-xs">Authorization: Bearer &lt;token&gt;</code>;
        a <strong>fleet</strong> token works before any agent has connected and is capped by expiry and
        agent count; a <strong>bound</strong> token <em>is</em> one agent's identity. The token is shown{" "}
        <strong>once</strong> — the server stores only its SHA-256 — so paste it somewhere safe when it
        is minted. A lost token costs one re-mint, not an outage.
      </p>
      <p className="text-sm">
        Mint and revoke under <Link to="/agent-tokens" className="text-[var(--color-accent)] hover:underline">Admin → Agent tokens</Link>{" "}
        (admin only).
      </p>

      <H2>Selecting agents with SQL</H2>
      <p className="max-w-prose text-sm">
        The <Link to="/" className="text-[var(--color-accent)] hover:underline">Agents</Link> search box
        and group queries speak one read-only language:{" "}
        <code className="font-mono text-xs">SELECT * FROM agents WHERE ...</code>. Suggestions come from
        the values this fleet actually reports (built from{" "}
        <code className="font-mono text-xs">GET /agents/query/schema</code>), so attribute columns are
        named exactly as agents report them — an unknown attribute matches nothing rather than failing
        the query. A group's query may not use <code className="font-mono text-xs">LIMIT</code> or{" "}
        <code className="font-mono text-xs">OFFSET</code>.
      </p>
      <p className="max-w-prose text-sm text-[var(--color-muted)]">
        Full language reference: <code className="font-mono text-xs">docs/sql-agent-queries.md</code>.
      </p>

      <H2>Remote config: propose → approve → canary → gate</H2>
      <p className="max-w-prose text-sm">
        The <Link to="/remote-config" className="text-[var(--color-accent)] hover:underline">Remote Config</Link>{" "}
        page is built around review, not a single push button: target → edit → validate → propose
        (delivers nothing) → approve &amp; roll out (approver). The canary batch releases first; the
        rest of the fleet is held until every canary agent reports <code className="font-mono text-xs">APPLIED</code>{" "}
        with our hash, stays healthy, and the health-gate window has elapsed. A redacted document is
        refused: pushing one would wipe the fleet's credentials.
      </p>

      <H2>Admin console security</H2>
      <ul className="max-w-prose list-disc space-y-1.5 pl-5 text-sm leading-relaxed text-[var(--color-muted)]">
        <li>
          Signing in posts your name and password to{" "}
          <code className="font-mono text-xs">POST /auth/session</code> once; the password is discarded
          and only an httpOnly session cookie is kept. Sessions expire on a server TTL and after
          inactivity; <em>Extend</em> is an audited re-auth that revokes the old session.
        </li>
        <li>
          <strong>Roles</strong> (viewer &lt; operator &lt; approver &lt; admin) decide which actions
          are offered. Hiding a button is cosmetic — the server re-checks your role on every request
          and records the action in the audit log.
        </li>
        <li>
          With no admin credential configured the server is <strong>read-only</strong>: reads work,
          every privileged action is refused.
        </li>
        <li>
          No secret is ever placed in a URL. Only non-secret identifiers appear in query parameters,
          because they land in proxy logs.
        </li>
      </ul>
    </section>
  );
}
