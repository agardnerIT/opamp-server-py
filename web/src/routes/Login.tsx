import { useState } from 'react'

// Foundations stub: layout and states only. The real submit posts to
// POST /api/auth/session once cookie-session support lands server-side.
export default function Login() {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)

  return (
    <div className="flex min-h-screen items-center justify-center">
      <form
        className="w-80 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6"
        onSubmit={(e) => {
          e.preventDefault()
          setError('Not wired yet — auth endpoint lands with cookie-session support.')
        }}
      >
        <h1 className="mb-1 text-lg font-semibold">OpAMP Console</h1>
        <p className="mb-5 text-sm text-[var(--color-muted)]">
          Sign in with the admin password to manage the fleet.
        </p>
        <label className="mb-1 block text-sm" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          autoFocus
          className="mb-4 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button
          type="submit"
          className="w-full rounded-md bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white hover:opacity-90"
        >
          Sign in
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
