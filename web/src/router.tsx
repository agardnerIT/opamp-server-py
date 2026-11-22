import {
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import Login from './routes/Login'
import AppShell from './routes/AppShell'
import Agents from './routes/Agents'
import AgentDetail from './routes/AgentDetail'
import Groups from './routes/Groups'
import RemoteConfig from './routes/RemoteConfig'
import Reports from './routes/Reports'
import Help from './routes/Help'
import Alerts from './routes/Alerts'
import Compliance from './routes/Compliance'
import AgentTokens from './routes/AgentTokens'
import Sessions from './routes/Sessions'
import Updates from './routes/Updates'
import ReleaseEmails from './routes/ReleaseEmails'
import NotFound from './routes/NotFound'
import RootError from './routes/RootError'

const rootRoute = createRootRoute({
  notFoundComponent: NotFound,
  errorComponent: RootError,
})

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: Login,
})

const shellRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'shell',
  component: AppShell,
})

const agentsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/',
  component: Agents,
  // The SQL selection rides in the URL (#146 parity): a detail page's back
  // link re-enters the same selection, and a filtered list is shareable.
  validateSearch: (search: Record<string, unknown>): { q?: string } => {
    return typeof search.q === 'string' && search.q.trim() !== '' ? { q: search.q } : {};
  },
})
const agentDetailRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agents/$agentId',
  component: AgentDetail,
  // The selection rides along (#146): the detail's back link re-enters it.
  validateSearch: (search: Record<string, unknown>): { q?: string } => {
    return typeof search.q === 'string' && search.q.trim() !== '' ? { q: search.q } : {};
  },
})
const reportsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/reports',
  component: Reports,
})
const groupsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/groups',
  component: Groups,
})
const remoteConfigRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/remote-config',
  component: RemoteConfig,
})
const helpRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/help',
  component: Help,
})
const alertsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/alerts',
  component: Alerts,
})
const complianceRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/compliance',
  component: Compliance,
})
const agentTokensRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agent-tokens',
  component: AgentTokens,
})
const sessionsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/sessions',
  component: Sessions,
})
const updatesRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/updates',
  component: Updates,
})
const releaseEmailsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/release-emails',
  component: ReleaseEmails,
})

const routeTree = rootRoute.addChildren([
  loginRoute,
  shellRoute.addChildren([
    agentsRoute,
    agentDetailRoute,
    reportsRoute,
    groupsRoute,
    remoteConfigRoute,
    helpRoute,
    alertsRoute,
    complianceRoute,
    agentTokensRoute,
    sessionsRoute,
    updatesRoute,
    releaseEmailsRoute,
  ]),
])

export const router = createRouter({ routeTree })
