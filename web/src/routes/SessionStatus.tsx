import { useEffect, useState } from "react";
import { Button } from "../components/ui/Button";
import { Link, useRouter } from "@tanstack/react-router";
import { ApiError } from "../api/errors";
import { useExtendSession, useLogout, useSession } from "../api/session";

/**
 * The AppShell session-status slot: identity, role, expiry, Extend, Logout.
 *
 * "Extend" is the audited re-auth path (`extendSession`): fresh credentials,
 * old session revoked, new one minted — never a client-side TTL extension.
 */

function minutesLeft(iso: string | null): string {
  if (!iso) return "no expiry";
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "unknown";
  if (ms <= 0) return "expired";
  const mins = Math.floor(ms / 60_000);
  if (mins >= 60) {
    const h = Math.floor(mins / 60);
    return `${h}h ${mins % 60}m left`;
  }
  return `${mins}m left`;
}

function ExtendForm({
  initialName,
  onDone,
  onCancel,
}: {
  initialName: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const extend = useExtendSession();
  const [name, setName] = useState(initialName);
  const [password, setPassword] = useState("");
  // Derived during render; a failed re-auth is surfaced inline, and if the
  // fresh login answered 401 the old session is already revoked — the global
  // unauthorized handler takes over from here.
  const error = extend.isError
    ? extend.error instanceof ApiError
      ? extend.error.message
      : String(extend.error)
    : null;

  useEffect(() => {
    if (extend.isSuccess) onDone();
  }, [extend.isSuccess, onDone]);

  return (
    <form
      className="card-2 mt-2 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        extend.mutate({ name, password });
      }}
    >
      <p className="mb-2 text-xs leading-relaxed text-[var(--color-muted)]">
        Re-authenticate: your current session is revoked and a fresh one is
        minted (audited).
      </p>
      <input
        aria-label="Name"
        type="text"
        autoComplete="username"
        className="field mb-2 text-xs"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <input
        aria-label="Password"
        type="password"
        autoComplete="current-password"
        className="field mb-2 text-xs"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        autoFocus
      />
      <div className="flex gap-2">
        <Button
          type="submit"
          variant="primary"
          size="sm"
          disabled={extend.isPending || password.length === 0}
        >
          {extend.isPending ? "Extending…" : "Extend"}
        </Button>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-[var(--color-danger)]">
          {error}
        </p>
      )}
    </form>
  );
}

export default function SessionStatus() {
  const router = useRouter();
  const session = useSession();
  const logout = useLogout();
  const [extending, setExtending] = useState(false);

  if (session.isPending) {
    return (
      <div className="mt-auto border-t border-[var(--color-border)] px-4 py-4 text-xs text-[var(--color-muted)]">
        Checking session…
      </div>
    );
  }

  // 401 → the global unauthorized handler routes to /login; this is a fallback.
  if (session.isError || !session.data) {
    return (
      <div className="mt-auto border-t border-[var(--color-border)] px-4 py-4 text-xs">
        <Link to="/login" className="text-[var(--color-accent)] hover:underline">
          Sign in
        </Link>
      </div>
    );
  }

  const me = session.data;
  if (!me.authenticated) {
    return (
      <div className="mt-auto border-t border-[var(--color-border)] px-4 py-4 text-xs leading-relaxed text-[var(--color-muted)]">
        Read-only session (no admin credential configured)
      </div>
    );
  }

  return (
    <div className="mt-auto border-t border-[var(--color-border)] px-4 py-4 text-xs">
      {extending ? (
        <ExtendForm
          initialName={me.method === "session" || me.method === "legacy" ? "" : me.name}
          onDone={() => setExtending(false)}
          onCancel={() => setExtending(false)}
        />
      ) : (
        <>
          <div className="text-[13px] font-medium text-[var(--color-text)]">{me.name}</div>
          <div className="mt-0.5 text-[var(--color-muted)]">
            role <span className="text-[var(--color-text)]">{me.role}</span> ·{" "}
            <span className="tnum">{minutesLeft(me.session_expires_at)}</span>
          </div>
          <div className="mt-2 flex gap-1.5">
            <Button variant="ghost" size="sm" onClick={() => setExtending(true)}>
              Extend
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={logout.isPending}
              onClick={() =>
                void logout.mutateAsync().then(() => {
                  void router.navigate({ to: "/login" });
                })
              }
            >
              {logout.isPending ? "Signing out…" : "Sign out"}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
