import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import { setUnauthorizedHandler } from './api/errors'
import './index.css'

// Retries are for transient failures only: a 4xx answer (403 role refusal, 404
// missing agent, 422 rejected body) is the server's decision, not bad luck, and
// retrying it just delays the honest error message the page renders.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        const status = (error as { status?: number }).status;
        if (status !== undefined && status < 500) return false;
        return failureCount < 3;
      },
    },
  },
})

/**
 * Global 401 behavior: a relogin-classified error means the httpOnly session
 * is gone (expired, idle-timed-out, revoked) — drop session state and show
 * login. A 401 from the login form itself does not route (we are already
 * there); it is surfaced as a form message by the page.
 */
setUnauthorizedHandler(() => {
  if (router.state.location.pathname !== '/login') {
    // Clear everything, not just the session query: data fetched under the
    // dead session must not linger, and the next login starts from zero
    // (same hygiene as useLogout / useLogin in api/session.ts).
    queryClient.clear()
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
