import {
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import Login from './routes/Login'
import AppShell from './routes/AppShell'
import Agents from './routes/Agents'
import Groups from './routes/Groups'
import RemoteConfig from './routes/RemoteConfig'
import Reports from './routes/Reports'

const rootRoute = createRootRoute()

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
const reportsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/reports',
  component: Reports,
})

const routeTree = rootRoute.addChildren([
  loginRoute,
  shellRoute.addChildren([agentsRoute, groupsRoute, remoteConfigRoute, reportsRoute]),
])

export const router = createRouter({ routeTree })
