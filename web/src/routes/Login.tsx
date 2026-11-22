import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "@tanstack/react-router";
import { getAuthStatus } from "../api/endpoints";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/errors";
import { useLogin } from "../api/session";

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
    <div className="w-96 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6">
      <h1 className="mb-1 text-lg font-semibold">OpAMP Console</h1>
      <p className="mt-3 text-sm text-[var(--color-muted)]">
        This server has no admin credential configured, so it is running in
        read-only mode. Set <code>ADMIN_PASSWORD</code> or create a principal,
        then restart the server to sign in.
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
    return <ReadOnlyNotice />
  }

  const needsName = status?.auth_mode === "principals"

  return (
    <div className="flex min-h-screen items-center justify-center">
      <form
        className="w-80 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6"
        onSubmit={onSubmit}
      >
        <h1 className="mb-1 text-lg font-semibold">OpAMP Console</h1>
        <p className="mb-5 text-sm text-[var(--color-muted)]">
          Sign in to manage the fleet.
        </p>
        {needsName && (
          <>
            <label className="mb-1 block text-sm" htmlFor="name">
              Name
            </label>
            <input
              id="name"
              type="text"
              autoComplete="username"
              autoFocus
              className="mb-4 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </>
        )}
        <label className="mb-1 block text-sm" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus={!needsName}
          className="mb-4 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button
          type="submit"
          disabled={login.isPending}
          className="w-full rounded-md bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
        {error && (
          <p role="alert" className="mt-3 text-sm text-[var(--color-danger)]">
            {error}
          </p>
        )}
      </form>
    </div>
  )
}
