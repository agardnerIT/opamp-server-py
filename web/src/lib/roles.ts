/**
 * Client-side mirror of the server role ladder (`server/auth.py`):
 * viewer < operator < approver < admin, higher roles satisfy lower ones.
 *
 * UI hint only (nav visibility, disabling actions) — the server's
 * `require_roles` is always the authority; never trust client gating.
 */

export type Role = "viewer" | "operator" | "approver" | "admin";

const ROLE_RANK: Record<Role, number> = {
  viewer: 1,
  operator: 2,
  approver: 3,
  admin: 4,
};

/** True when `role` satisfies at least `min` on the server's ladder. */
export function roleAtLeast(
  role: string | null | undefined,
  min: Role,
): boolean {
  if (!role) return false;
  const rank = ROLE_RANK[role as Role];
  return rank !== undefined && rank >= ROLE_RANK[min];
}
