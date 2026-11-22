import { Link, Outlet, useMatchRoute } from '@tanstack/react-router'

const NAV = [
  { to: '/', label: 'Agents' },
  { to: '/groups', label: 'Groups' },
  { to: '/remote-config', label: 'Remote Config' },
  { to: '/reports', label: 'Reports' },
] as const

export default function AppShell() {
  const match = useMatchRoute()

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-52 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="px-4 py-4 text-sm font-semibold">OpAMP Console</div>
        <nav className="flex flex-col gap-1 px-2">
          {NAV.map(({ to, label }) => {
            const active = match({ to, fuzzy: true })
            return (
              <Link
                key={to}
                to={to}
                className={
                  'rounded-md px-3 py-2 text-sm ' +
                  (active
                    ? 'bg-[var(--color-bg)] font-medium text-[var(--color-text)]'
                    : 'text-[var(--color-muted)] hover:text-[var(--color-text)]')
                }
              >
                {label}
              </Link>
            )
          })}
        </nav>
        <div className="mt-auto px-4 py-4 text-xs text-[var(--color-muted)]">
          session status stub
        </div>
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
