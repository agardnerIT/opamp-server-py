import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  listAgentTokens,
  mintFleetToken,
  revokeAgentToken,
  type AgentTokenRow,
} from "../api/admin";
import { getAgentTypes } from "../api/endpoints";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { Section } from "../components/Section";

/**
 * Agent tokens (new-ui stage 8): mint a fleet token (shown once — the server
 * stores only its hash) and revoke issued ones. Agent auth is mandatory
 * (#125), so the mint form is the page's headline, not something hidden in a
 * disclosure: an operator arrives here either to mint the first credential or
 * to replace one they just revoked.
 *
 * Table columns match `agent_token_rows` in `ui/shared.py` (#126): state,
 * label, profile the snippet was minted for (#144), handle, cap usage.
 */

const buttonClass =
  "rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:text-[var(--color-text)] disabled:opacity-40 disabled:hover:text-inherit";
const inputClass =
  "rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm";

function tokenState(t: AgentTokenRow): string {
  if (t.revoked_at) return "revoked";
  if (t.expired) return "expired";
  return t.active ? "active" : "inactive";
}

export default function AgentTokens() {
  const session = useSession();
  const canAdmin = roleAtLeast(session.data?.role, "admin");
  const queryClient = useQueryClient();

  const tokens = useQuery({ queryKey: ["agent-tokens"], queryFn: listAgentTokens });
  const types = useQuery({ queryKey: ["agent-types"], queryFn: getAgentTypes, staleTime: 5 * 60_000 });

  // Mint form. All fields optional server-side; empty text = omit.
  const [label, setLabel] = useState("");
  const [agentType, setAgentType] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [maxAgents, setMaxAgents] = useState("");
  const [minted, setMinted] = useState<Awaited<ReturnType<typeof mintFleetToken>> | null>(null);
  const [mintError, setMintError] = useState<string | null>(null);

  const [revokeId, setRevokeId] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const mint = useMutation({
    mutationFn: () =>
      mintFleetToken({
        label: label.trim() || undefined,
        agent_type: agentType || undefined,
        expires_in_days: expiresDays ? Number(expiresDays) : undefined,
        max_agents: maxAgents ? Number(maxAgents) : undefined,
      }),
    onSuccess: (result) => {
      setMinted(result);
      setMintError(null);
      setLabel("");
      setExpiresDays("");
      setMaxAgents("");
      void queryClient.invalidateQueries({ queryKey: ["agent-tokens"] });
    },
    onError: (err) => setMintError(String(err)),
  });

  const revoke = useMutation({
    mutationFn: (tokenId: string) => revokeAgentToken(tokenId),
    onSuccess: () => {
      setRevokeId(null);
      setRevokeError(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-tokens"] });
    },
    onError: (err) => setRevokeError(String(err)),
  });

  return (
    <section className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Agent tokens</h1>
        <p className="mt-1 max-w-prose text-xs text-[var(--color-muted)]">
          The credentials agents present on <code>/v1/opamp</code>. A fleet
          token is not tied to an agent id and serves up to its cap.
        </p>
      </div>

      <Section
        title="Mint a token"
        caption="The plaintext token is shown once — the server keeps only its hash."
      >
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            mint.mutate();
          }}
        >
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Label</span>
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className={inputClass}
              placeholder="prod"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">For agent type</span>
            <select
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              className={inputClass}
            >
              <option value="">default</option>
              {(types.data?.types ?? []).map((t) => (
                <option key={t.name} value={t.name}>
                  {t.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Expires (days)</span>
            <input
              type="number"
              min={1}
              max={365}
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
              className={`${inputClass} w-28`}
              placeholder="never"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Agent cap</span>
            <input
              type="number"
              min={1}
              value={maxAgents}
              onChange={(e) => setMaxAgents(e.target.value)}
              className={`${inputClass} w-28`}
              placeholder="unlimited"
            />
          </label>
          <button type="submit" className={buttonClass} disabled={mint.isPending || !canAdmin}>
            Mint token
          </button>
        </form>
        {!canAdmin && (
          <p className="mt-2 text-xs text-[var(--color-muted)]">
            Minting needs the admin role.
          </p>
        )}
        {mintError && (
          <p role="alert" className="mt-2 text-xs text-[var(--color-danger)]">
            {mintError}
          </p>
        )}
        {minted && (
          <div className="mt-3 rounded-md border border-[var(--color-accent)] p-3 text-xs">
            <p className="font-medium text-[var(--color-warn)]">
              Copy it now — it will not be shown again.
            </p>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all font-mono text-sm">
              {minted.token}
            </pre>
            <p className="mt-2 text-[var(--color-muted)]">Snippet for the agents' config:</p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-[var(--color-muted)]">
              {minted.collector_snippet}
            </pre>
          </div>
        )}
      </Section>

      <Section title="Issued tokens" caption="A revoked token blocks its agents immediately; their state and config are kept.">
        {tokens.isPending && <p className="text-sm text-[var(--color-muted)]">Loading…</p>}
        {tokens.isError && <EmptyState title="Could not load tokens" detail={String(tokens.error)} />}
        {tokens.data && tokens.data.tokens.length === 0 && (
          <EmptyState
            title="No tokens yet"
            detail="Agents cannot connect until one exists — mint the first one above, or with opampctl agent-tokens create."
          />
        )}
        {tokens.data && tokens.data.tokens.length > 0 && (
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-[var(--color-muted)]">
                <th className="py-1 pr-3 font-medium">State</th>
                <th className="py-1 pr-3 font-medium">Label</th>
                <th className="py-1 pr-3 font-medium">For</th>
                <th className="py-1 pr-3 font-medium">Token</th>
                <th className="py-1 pr-3 font-medium">Agents</th>
                <th className="py-1 pr-3 font-medium">Expires</th>
                <th className="py-1 font-medium" aria-label="actions" />
              </tr>
            </thead>
            <tbody>
              {tokens.data.tokens.map((t) => (
                <tr key={t.token_id} className="border-t border-[var(--color-border)]">
                  <td className="py-1.5 pr-3">{tokenState(t)}</td>
                  <td className="py-1.5 pr-3">{t.label ?? "—"}</td>
                  <td className="py-1.5 pr-3">{t.agent_type ?? "—"}</td>
                  <td className="py-1.5 pr-3 font-mono">{t.token_id}</td>
                  <td className="py-1.5 pr-3">
                    {t.agents_used} of {t.max_agents ?? "unlimited"}
                  </td>
                  <td className="py-1.5 pr-3">{t.expires_at ? t.expires_at.slice(0, 10) : "never"}</td>
                  <td className="py-1.5 text-right">
                    {!t.revoked_at && !t.expired && (
                      <button
                        type="button"
                        onClick={() => {
                          setRevokeId(t.token_id);
                          setRevokeError(null);
                        }}
                        disabled={!canAdmin}
                        className={buttonClass}
                        title={canAdmin ? "Revoke this token" : "Revoking needs the admin role"}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Modal open={revokeId !== null} title={`Revoke ${revokeId}?`} onClose={() => setRevokeId(null)}>
        <p className="mb-4 max-w-prose text-xs text-[var(--color-muted)]">
          This blocks those agents immediately. Their state and config are kept,
          and the token cannot be un-revoked — mint a replacement if it was a
          mistake.
        </p>
        {revokeError && (
          <p role="alert" className="mb-2 text-xs text-[var(--color-danger)]">
            {revokeError}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" className={buttonClass} onClick={() => setRevokeId(null)}>
            Cancel
          </button>
          <button
            type="button"
            className="rounded-md border border-[var(--color-danger)] px-3 py-1.5 text-xs font-medium text-[var(--color-danger)]"
            disabled={revoke.isPending}
            onClick={() => revokeId && revoke.mutate(revokeId)}
          >
            Revoke token
          </button>
        </div>
      </Modal>
    </section>
  );
}
