import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { listAdminSessions, revokeAdminSession } from "../api/admin";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";

/**
 * Sessions (new-ui stage 8): every admin session the server honours,
 * including other browsers. Revoke is per row (the id is right there in the
 * table — Streamlit made the operator retype it, which only invited typos);
 * revoking is audited against the acting principal.
 */

const buttonClass =
  "rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:text-[var(--color-text)] disabled:opacity-40 disabled:hover:text-inherit";

export default function Sessions() {
  const session = useSession();
  const canAdmin = roleAtLeast(session.data?.role, "admin");
  const queryClient = useQueryClient();

  const sessions = useQuery({ queryKey: ["admin-sessions"], queryFn: listAdminSessions });
  const [revokeId, setRevokeId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const revoke = useMutation({
    mutationFn: (sessionId: string) => revokeAdminSession(sessionId),
    onSuccess: () => {
      setRevokeId(null);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["admin-sessions"] });
    },
    onError: (err) => setError(String(err)),
  });

  const rows = sessions.data?.sessions ?? [];
  const target = rows.find((r) => r.session_id === revokeId);

  return (
    <section className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Sessions</h1>
        <p className="mt-1 max-w-prose text-xs text-[var(--color-muted)]">
          Every admin session the server currently honours, including other
          browsers. Revoking one is audited against your principal.
        </p>
      </div>

      {sessions.isPending && <p className="text-sm text-[var(--color-muted)]">Loading…</p>}
      {sessions.isError && <EmptyState title="Could not load sessions" detail={String(sessions.error)} />}
      {sessions.data && rows.length === 0 && (
        <EmptyState
          title="No sessions"
          detail="Sessions opened with the shared legacy password may not appear here — the server hides nothing else."
        />
      )}
      {rows.length > 0 && (
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-[var(--color-muted)]">
              <th className="py-1 pr-3 font-medium">Session</th>
              <th className="py-1 pr-3 font-medium">Principal</th>
              <th className="py-1 pr-3 font-medium">Started</th>
              <th className="py-1 pr-3 font-medium">Last seen</th>
              <th className="py-1 pr-3 font-medium">Expires</th>
              <th className="py-1 pr-3 font-medium">Client IP</th>
              <th className="py-1 font-medium" aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.session_id} className="border-t border-[var(--color-border)]">
                <td className="py-1.5 pr-3 font-mono">{r.session_id}</td>
                <td className="py-1.5 pr-3">
                  {r.principal || r.principal_id || "—"}
                  {r.expired && <span className="ml-2 text-[var(--color-muted)]">expired</span>}
                </td>
                <td className="py-1.5 pr-3">{r.created_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                <td className="py-1.5 pr-3">{r.last_seen_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                <td className="py-1.5 pr-3">{r.expires_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                <td className="py-1.5 pr-3 font-mono">{r.client_ip ?? "—"}</td>
                <td className="py-1.5 text-right">
                  {!r.expired && (
                    <button
                      type="button"
                      onClick={() => {
                        setRevokeId(r.session_id);
                        setError(null);
                      }}
                      disabled={!canAdmin}
                      className={buttonClass}
                      title={canAdmin ? "Revoke this session" : "Revoking needs the admin role"}
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

      <Modal open={revokeId !== null} title={`Revoke session ${revokeId}?`} onClose={() => setRevokeId(null)}>
        <p className="mb-4 max-w-prose text-xs text-[var(--color-muted)]">
          {target
            ? `${target.principal || target.principal_id || "This session"}'s browser signs out on its next request.`
            : "That browser signs out on its next request."}
        </p>
        {error && (
          <p role="alert" className="mb-2 text-xs text-[var(--color-danger)]">
            {error}
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
            Revoke session
          </button>
        </div>
      </Modal>
    </section>
  );
}
