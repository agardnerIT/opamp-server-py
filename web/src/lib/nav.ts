/**
 * Sidebar definition. It started as a mirror of the retired Streamlit
 * `st.navigation` tree (new-ui stage 4) so every page stayed reachable during
 * the migration; since 2027-ui Stage B the main section intentionally diverges:
 * `/` is the Overview landing page and the agents table lives at `/agents` (the
 * owner's "more generic landing page" ask). URLs stay flat, and the Admin
 * section keeps the same names the Streamlit pages used.
 */

import { roleAtLeast, type Role } from "./roles";

export type NavItem = {
  to: string;
  label: string;
  /** Minimum role hint; the server still enforces. Omitted = all roles. */
  minRole?: Role;
};

export const MAIN_NAV: NavItem[] = [
  { to: "/", label: "Overview" },
  { to: "/agents", label: "Agents" },
  { to: "/reports", label: "Reports" },
  { to: "/groups", label: "Groups" },
  { to: "/remote-config", label: "Remote Config" },
  { to: "/help", label: "Help" },
];

export const ADMIN_NAV: NavItem[] = [
  { to: "/alerts", label: "Alerts", minRole: "operator" },
  { to: "/compliance", label: "Compliance", minRole: "operator" },
  { to: "/agent-tokens", label: "Agent tokens", minRole: "admin" },
  { to: "/sessions", label: "Sessions", minRole: "admin" },
  { to: "/updates", label: "Updates", minRole: "operator" },
  // Release emails folded into Updates (stage 8): the signup form lives on
  // the project's update Worker, which the REST API does not expose.
];

/** Minimum role for the whole Admin section (its least-privileged page). */
export const ADMIN_SECTION_MIN_ROLE: Role = "operator";

/** Items of `nav` the given role may see (hint only; server enforces). */
export function visibleNav(
  nav: NavItem[],
  role: string | null | undefined,
): NavItem[] {
  return nav.filter((item) => !item.minRole || roleAtLeast(role, item.minRole));
}
