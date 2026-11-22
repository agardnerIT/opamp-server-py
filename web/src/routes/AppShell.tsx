import { Link, Outlet, useMatchRoute, useRouterState } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'
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

function NavLinks({
  items,
  match,
  onNavigate,
}: {
  items: NavItem[]
  match: ReturnType<typeof useMatchRoute>
  /** Drawer context: navigation closes the slide-over. */
  onNavigate?: () => void
}) {
  return items.map(({ to, label }) => {
    // '/' is an ancestor of every route, so fuzzy matching would mark Overview
    // active everywhere; it gets an exact match, the rest get fuzzy.
    const active = to === '/' ? !!match({ to }) : !!match({ to, fuzzy: true })
    return (
      <Link
        key={to}
        to={to}
        aria-current={active ? 'page' : undefined}
        onClick={onNavigate}
        className={
          'relative flex h-11 items-center rounded-[var(--radius-sm)] px-3 text-[13px] font-medium transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] md:h-8 ' +
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

function SidebarContent({
  match,
  adminItems,
  onNavigate,
}: {
  match: ReturnType<typeof useMatchRoute>
  adminItems: NavItem[]
  onNavigate?: () => void
}) {
  return (
    <div className="flex h-full flex-col">
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
        <NavLinks items={MAIN_NAV} match={match} onNavigate={onNavigate} />
      </nav>
      {adminItems.length > 0 && (
        <>
          <div className="mt-5 px-4 text-xs font-medium text-[var(--color-faint)]">
            Admin
          </div>
          <nav aria-label="Admin" className="mt-1 flex flex-col gap-0.5 px-3">
            <NavLinks items={adminItems} match={match} onNavigate={onNavigate} />
          </nav>
        </>
      )}
      <SessionStatus />
    </div>
  )
}

export default function AppShell() {
  const match = useMatchRoute()
  const session = useSession()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const [navOpen, setNavOpen] = useState(false)
  const drawerRef = useRef<HTMLDivElement>(null)

  // Any navigation closes the drawer. Derived during render (the adjust-state
  // pattern) so an effect is not needed; also covers browser back/forward.
  const [lastPath, setLastPath] = useState(pathname)
  if (pathname !== lastPath) {
    setLastPath(pathname)
    if (navOpen) setNavOpen(false)
  }

  // Client-side hint only: the server's require_roles stays the authority.
  // While the session query is pending the Admin section stays hidden — a
  // low-role flash is worse than a beat without the section.
  const me = session.data
  const canSeeAdmin =
    !!me?.authenticated && roleAtLeast(me.role, ADMIN_SECTION_MIN_ROLE)
  const adminItems = canSeeAdmin ? visibleNav(ADMIN_NAV, me?.role) : []

  // Esc behaves like the overlay click.
  useEffect(() => {
    if (!navOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setNavOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])
  useEffect(() => {
    if (navOpen) drawerRef.current?.focus()
  }, [navOpen])

  return (
    <div className="flex min-h-screen">
      {/* Mobile top bar: menu button + wordmark. The drawer below carries the
          same two labelled navs, so e2e keeps querying them by name. */}
      <div className="fixed inset-x-0 top-0 z-30 flex h-14 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 md:hidden">
        <button
          type="button"
          aria-label={navOpen ? 'Close navigation' : 'Open navigation'}
          aria-expanded={navOpen}
          aria-controls="app-drawer"
          onClick={() => setNavOpen((v) => !v)}
          className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-sm)] text-[var(--color-muted)] transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]"
        >
          <svg aria-hidden width="18" height="18" viewBox="0 0 18 18" fill="none">
            {navOpen ? (
              <path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            ) : (
              <path d="M2 5h14M2 9h14M2 13h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            )}
          </svg>
        </button>
        <span
          aria-hidden
          className="inline-block h-6 w-6 rounded-[var(--radius-sm)] bg-[linear-gradient(135deg,var(--color-accent-deep),var(--color-accent-bright))]"
        />
        <span className="font-display text-[15px] font-semibold tracking-[-0.01em]">
          OpAMP Console
        </span>
      </div>

      {/* Slide-over drawer (mobile only): overlay + the sidebar panel. */}
      {navOpen && (
        <div id="app-drawer" className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
            className="absolute inset-0 bg-black/60"
          />
          <aside
            ref={drawerRef}
            tabIndex={-1}
            aria-label="Navigation"
            className="absolute bottom-0 left-0 top-0 w-64 overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-surface)] shadow-[var(--shadow-overlay)] outline-none"
          >
            <SidebarContent match={match} adminItems={adminItems} onNavigate={() => setNavOpen(false)} />
          </aside>
        </div>
      )}

      {/* Desktop sidebar, unchanged structure. */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)] md:flex">
        <SidebarContent match={match} adminItems={adminItems} />
      </aside>

      <main className="min-w-0 flex-1 overflow-auto px-4 pb-10 pt-20 md:px-8 md:pb-0 md:pt-7">
        <div className="mx-auto max-w-5xl">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
