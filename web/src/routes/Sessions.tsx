import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { listAdminSessions, revokeAdminSession } from "../api/admin";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Button, buttonStyles } from "../components/ui/Button";

/**
 * Sessions (new-ui stage 8): every admin session the server honours,
 * including other browsers. Revoke is per row (the id is right there in the
 * table — Streamlit made the operator retype it, which only invited typos);
 * revoking is audited against the acting principal.
 */

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
      <PageHeader
        title="Sessions"
        lede="Every admin session the server currently honours, including other browsers. Revoking one is audited against your principal."
      />

      {sessions.isPending && <p className="text-sm text-[var(--color-muted)]">Loading…</p>}
      {sessions.isError && <EmptyState title="Could not load sessions" detail={String(sessions.error)} />}
      {sessions.data && rows.length === 0 && (
        <EmptyState
          title="No sessions"
          detail="Sessions opened with the shared legacy password may not appear here — the server hides nothing else."
        />
      )}
      {rows.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Principal</th>
              <th>Started</th>
              <th>Last seen</th>
              <th>Expires</th>
              <th>Client IP</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.session_id}>
                <td className="font-mono text-xs">{r.session_id}</td>
                <td>
                  {r.principal || r.principal_id || "—"}
                  {r.expired && <span className="ml-2 text-xs text-[var(--color-faint)]">expired</span>}
                </td>
                <td className="tnum text-xs">{r.created_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                <td className="tnum text-xs">{r.last_seen_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                <td className="tnum text-xs">{r.expires_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                <td className="font-mono text-xs">{r.client_ip ?? "—"}</td>
                <td className="text-right">
                  {!r.expired && (
                    <button
                      type="button"
                      onClick={() => {
                        setRevokeId(r.session_id);
                        setError(null);
                      }}
                      disabled={!canAdmin}
                      className={buttonStyles({ variant: "ghost", size: "sm" })}
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
          <Button variant="ghost" onClick={() => setRevokeId(null)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            disabled={revoke.isPending}
            onClick={() => revokeId && revoke.mutate(revokeId)}
          >
            Revoke session
          </Button>
        </div>
      </Modal>
    </section>
  );
}
