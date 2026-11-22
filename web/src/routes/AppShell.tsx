import { Link, Outlet, useMatchRoute } from '@tanstack/react-router'
import SessionStatus from './SessionStatus'
import {
  ADMIN_NAV,
  ADMIN_SECTION_MIN_ROLE,
  MAIN_NAV,
  visibleNav,
  type NavItem,
} from '../lib/nav'
import { roleAtLeast } from '../lib/roles'
import { useSession } from '../api/session'

function NavLinks({ items, match }: { items: NavItem[]; match: ReturnType<typeof useMatchRoute> }) {
  return items.map(({ to, label }) => {
    // '/' is an ancestor of every route, so fuzzy matching would mark Agents
    // active everywhere; it gets an exact match, the rest get fuzzy.
    const active = to === '/' ? !!match({ to }) : !!match({ to, fuzzy: true })
    return (
      <Link
        key={to}
        to={to}
        aria-current={active ? 'page' : undefined}
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
  })
}

export default function AppShell() {
  const match = useMatchRoute()
  const session = useSession()

  // Client-side hint only: the server's require_roles stays the authority.
  // While the session query is pending the Admin section stays hidden — a
  // low-role flash is worse than a beat without the section.
  const me = session.data
  const canSeeAdmin =
    !!me?.authenticated && roleAtLeast(me.role, ADMIN_SECTION_MIN_ROLE)
  const adminItems = canSeeAdmin ? visibleNav(ADMIN_NAV, me?.role) : []

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-52 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="px-4 py-4 text-sm font-semibold">OpAMP Console</div>
        <nav aria-label="Main" className="flex flex-col gap-1 px-2">
          <NavLinks items={MAIN_NAV} match={match} />
        </nav>
        {adminItems.length > 0 && (
          <>
            <div className="mt-4 px-4 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              Admin
            </div>
            <nav aria-label="Admin" className="mt-1 flex flex-col gap-1 px-2">
              <NavLinks items={adminItems} match={match} />
            </nav>
          </>
        )}
        <SessionStatus />
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
