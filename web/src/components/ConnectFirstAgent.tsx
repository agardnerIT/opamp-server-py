import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAgentTypes, listAgents } from "../api/endpoints";
import { mintFleetToken, type MintedToken } from "../api/admin";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { Button } from "./ui/Button";
import { CopyButton } from "./ui/CopyButton";
import { Notice, useToast } from "./ui/Toast";

/**
 * The zero-agent hand-off (owner, 2027-01): "when there are no agents
 * connected I want generate API token right there". One click from landing on
 * the console to holding a credential — no trip through Agent tokens, whose
 * page carries the full table and every knob.
 *
 * Kept deliberately short: label (optional), agent type (drives which snippet
 * the server hands back), one primary button. Expiry, agent cap, revocation and
 * the issued list stay on Agent tokens, linked once in the footer.
 *
 * The panel polls `GET /agents` every 5s while it is open, so the first agent
 * to check in makes this view collapse into the connected Overview on its own —
 * the operator can watch the token they just minted become a fleet member.
 */
export function ConnectFirstAgent() {
  const session = useSession();
  const canMint = roleAtLeast(session.data?.role, "admin");
  const queryClient = useQueryClient();
  const toast = useToast();

  // Same key as Overview's query: one cache, two observers, and this one polls
  // fast because landing here means the fleet is empty and that is about to change.
  const agents = useQuery({
    queryKey: ["agents", {}],
    queryFn: () => listAgents(),
    refetchInterval: 5_000,
  });
  const types = useQuery({
    queryKey: ["agent-types"],
    queryFn: getAgentTypes,
    staleTime: 5 * 60_000,
  });

  const [label, setLabel] = useState("");
  const [agentType, setAgentType] = useState("");
  const [minted, setMinted] = useState<MintedToken | null>(null);
  const [mintError, setMintError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const mint = useMutation({
    mutationFn: () =>
      mintFleetToken({
        label: label.trim() || undefined,
        agent_type: agentType || undefined,
      }),
    onSuccess: (result) => {
      setMinted(result);
      setMintError(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-tokens"] });
    },
    onError: (err) => setMintError(String(err)),
  });

  // Announce the arrival exactly once (this panel only mounts on an empty fleet).
  const announced = useRef(false);
  useEffect(() => {
    const first = agents.data?.agents[0];
    if (!first || announced.current) return;
    announced.current = true;
    toast("ok", `${first.name ?? first.id.slice(0, 8)} connected.`);
  }, [agents.data, toast]);

  return (
    <section className="card flex flex-col gap-4 p-4" aria-labelledby="connect-first-agent">
      <header>
        <h2 id="connect-first-agent" className="text-sm font-semibold">
          {minted ? "Token ready" : "Generate an API token"}
        </h2>
        <p className="mt-0.5 text-xs leading-relaxed text-[var(--color-muted)]">
          {minted
            ? "Paste it into your agent's config. This page updates the moment one connects."
            : "Agents authenticate with it. Shown once — the server keeps only its hash."}
        </p>
      </header>

      {!minted && (
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            mint.mutate();
          }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Label</span>
              <input
                type="text"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className="field"
                placeholder="optional, e.g. prod"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Agent type</span>
              <select
                value={agentType}
                onChange={(e) => setAgentType(e.target.value)}
                className="field"
              >
                <option value="">default</option>
                {(types.data?.types ?? []).map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.display_name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <Button type="submit" variant="primary" size="lg" disabled={mint.isPending || !canMint}>
            {mint.isPending ? "Generating…" : "Generate API token"}
          </Button>

          {!canMint && (
            <p className="text-xs text-[var(--color-muted)]">
              Minting needs the admin role. Ask an admin, or run{" "}
              <code>opampctl agent-tokens create</code> on the server host.
            </p>
          )}
          {mintError && <Notice kind="error">{mintError}</Notice>}
        </form>
      )}

      {minted && (
        <div className="flex flex-col gap-3">
          <div className="card-2 p-3">
            <div className="flex items-start justify-between gap-2">
              <p className="flex items-center gap-2 text-[13px] font-medium text-[var(--color-warn)]">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
                  <path d="M8 1.8l6 3v4.2c0 3-2.5 5-6 5.7-3.5-.7-6-2.7-6-5.7V4.8l6-3z" strokeLinejoin="round" />
                </svg>
                Copy it now — it is not shown again.
              </p>
              <CopyButton value={minted.token} label="Copy token" />
            </div>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded-[var(--radius-sm)] bg-[var(--color-bg)] p-2.5 font-mono text-xs">
              {minted.token}
            </pre>
            <p className="mt-2 text-[11.5px] text-[var(--color-muted)]">
              Agents connect to{" "}
              <code className="text-[var(--color-text)]">{minted.endpoint}</code>
            </p>
          </div>

          <div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium">Config snippet</span>
              <CopyButton value={minted.collector_snippet} label="Copy snippet" variant="ghost" />
            </div>
            <pre className="mt-1 max-h-40 overflow-auto rounded-[var(--radius-sm)] border border-[var(--color-border)] bg-[var(--color-bg)] p-2.5 font-mono text-[11.5px] leading-relaxed text-[var(--color-muted)]">
              {minted.collector_snippet}
            </pre>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--color-border)] pt-3">
            <span className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
              <span aria-hidden className="topology-wait h-1.5 w-1.5 rounded-full bg-[var(--color-accent-bright)]" />
              Waiting for the first agent to check in…
            </span>
            <Button
              variant="ghost"
              size="sm"
              disabled={checking}
              onClick={() => {
                setChecking(true);
                void agents.refetch().finally(() => setChecking(false));
              }}
            >
              Check again
            </Button>
          </div>
        </div>
      )}

      <footer className="text-[11.5px] text-[var(--color-muted)]">
        Expiry, agent cap and revocation live on{" "}
        <Link
          to="/agent-tokens"
          className="text-[var(--color-accent)] underline decoration-[var(--color-border-strong)] underline-offset-2 hover:text-[var(--color-accent-strong)]"
        >
          Agent tokens
        </Link>
        .
      </footer>
    </section>
  );
}
