import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import { setUnauthorizedHandler } from './api/errors'
import { SESSION_QUERY_KEY } from './api/session'
import './index.css'

const queryClient = new QueryClient()

/**
 * Global 401 behavior: a relogin-classified error means the httpOnly session
 * is gone (expired, idle-timed-out, revoked) — drop session state and show
 * login. A 401 from the login form itself does not route (we are already
 * there); it is surfaced as a form message by the page.
 */
setUnauthorizedHandler(() => {
  if (router.state.location.pathname !== '/login') {
    queryClient.removeQueries({ queryKey: SESSION_QUERY_KEY })
    void router.navigate({ to: '/login' })
  }
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} context={{ queryClient }} />
    </QueryClientProvider>
  </StrictMode>,
)
