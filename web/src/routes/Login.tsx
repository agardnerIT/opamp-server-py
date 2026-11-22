import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "@tanstack/react-router";
import { getAuthStatus } from "../api/endpoints";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/errors";
import { useLogin } from "../api/session";
import { Button } from "../components/ui/Button";
import { Notice } from "../components/ui/Toast";

/**
 * Sign-in page. Posts to `POST /auth/session`; the server sets the httpOnly
 * session cookie and this page never sees or stores the token. Error text
 * follows the server contract: 401 = wrong credentials (the session is not
 * "dead" — there is none), 429 = lockout with the real wait, 403/read-only =
 * server has no credential configured.
 */

function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === "locked-out") {
      return error.message; // includes the Retry-After wait
    }
    if (error.kind === "unauthorized") {
      return "Incorrect name or password.";
    }
    if (error.kind === "read-only") {
      return error.message;
    }
    return error.message;
  }
  return String(error);
}

function ReadOnlyNotice() {
  return (
    <div className="card w-96 max-w-full p-6">
      <div className="mb-4 flex items-center gap-2.5">
        <span
          aria-hidden
          className="inline-block h-8 w-8 rounded-[var(--radius-sm)] bg-[linear-gradient(135deg,var(--color-accent-deep),var(--color-accent-bright))] shadow-[var(--inner-highlight)]"
        />
        <h1 className="font-display text-lg font-semibold">OpAMP Console</h1>
      </div>
      <p className="text-[13px] leading-relaxed text-[var(--color-muted)]">
        This server has no admin credential configured, so it is running in
        read-only mode. Set <code className="font-mono">ADMIN_PASSWORD</code> or
        create a principal, then restart the server to sign in.
      </p>
    </div>
  );
}

export default function Login() {
  const router = useRouter()
  const login = useLogin()
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  // Derived during render — a failed login is 401, but the page is already
  // the login page, so the global unauthorized handler must not route.
  const error = login.isError ? loginErrorMessage(login.error) : null

  const statusQuery = useQuery({
    queryKey: ["auth-status"],
    queryFn: getAuthStatus,
  })
  const status = statusQuery.data

  // If a live cookie survives (back-button to /login while signed in), go
  // straight to the console once the session query resolves.
  useEffect(() => {
    if (login.isSuccess) {
      void router.navigate({ to: "/" })
    }
  }, [login.isSuccess, router])

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    login.mutate({ name, password })
  }

  if (status && status.auth_mode === "disabled") {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <ReadOnlyNotice />
      </div>
    )
  }

  const needsName = status?.auth_mode === "principals"

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden p-6">
      {/* One quiet brand moment: a faint indigo glow behind the card. */}
      <div
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-[-20%] h-[60vh] w-[80vw] -translate-x-1/2 rounded-full opacity-40"
        style={{
          background:
            "radial-gradient(closest-side, rgb(64 81 181 / 0.35), transparent)",
        }}
      />
      <form
        className="card relative w-96 max-w-full p-7 shadow-[var(--shadow-overlay)]"
        onSubmit={onSubmit}
      >
        <div className="mb-6 flex items-center gap-3">
          <span
            aria-hidden
            className="inline-block h-9 w-9 rounded-[var(--radius-md)] bg-[linear-gradient(135deg,var(--color-accent-deep),var(--color-accent-bright))] shadow-[var(--inner-highlight)]"
          />
          <div>
            <h1 className="font-display text-lg font-semibold leading-tight">
              OpAMP Console
            </h1>
            <p className="text-xs text-[var(--color-muted)]">
              Sign in to manage the fleet
            </p>
          </div>
        </div>
        {needsName && (
          <>
            <label className="mb-1 block text-[13px] font-medium" htmlFor="name">
              Name
            </label>
            <input
              id="name"
              type="text"
              autoComplete="username"
              autoFocus
              className="field mb-4"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </>
        )}
        <label className="mb-1 block text-[13px] font-medium" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus={!needsName}
          className="field mb-5"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <Button
          type="submit"
          variant="primary"
          size="lg"
          disabled={login.isPending}
          className="w-full"
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </Button>
        {error && (
          <Notice kind="error" className="mt-4">
            {error}
          </Notice>
        )}
      </form>
    </div>
  )
}
