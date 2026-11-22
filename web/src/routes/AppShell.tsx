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
          'relative flex h-8 items-center rounded-[var(--radius-sm)] px-3 text-[13px] font-medium transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] ' +
          (active
            ? 'bg-[var(--color-surface-2)] text-[var(--color-text)]'
            : 'text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]/60 hover:text-[var(--color-text)]')
        }
      >
        {active && (
          <span
            aria-hidden
            className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-[var(--color-accent-bright)]"
          />
        )}
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
      <aside className="flex w-56 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="flex items-center gap-2.5 px-4 py-5">
          <span
            aria-hidden
            className="inline-block h-7 w-7 shrink-0 rounded-[var(--radius-sm)] bg-[linear-gradient(135deg,var(--color-accent-deep),var(--color-accent-bright))] shadow-[var(--inner-highlight)]"
          />
          <span className="font-display text-[15px] font-semibold tracking-[-0.01em]">
            OpAMP Console
          </span>
        </div>
        <nav aria-label="Main" className="flex flex-col gap-0.5 px-3">
          <NavLinks items={MAIN_NAV} match={match} />
        </nav>
        {adminItems.length > 0 && (
          <>
            <div className="mt-5 px-4 text-xs font-medium text-[var(--color-faint)]">
              Admin
            </div>
            <nav aria-label="Admin" className="mt-1 flex flex-col gap-0.5 px-3">
              <NavLinks items={adminItems} match={match} />
            </nav>
          </>
        )}
        <SessionStatus />
      </aside>
      <main className="min-w-0 flex-1 overflow-auto px-8 py-7">
        <div className="mx-auto max-w-5xl">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
