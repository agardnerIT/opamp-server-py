/**
 * Session state for the browser UI, backed by the httpOnly cookie the server
 * sets on `POST /auth/session` (new-ui stage 3). The token itself is never in
 * JS: identity comes from `GET /auth/me`, so a refresh survives and "still
 * logged in?" is one query, not stored state.
 *
 * 401 from `getMe` means "not signed in (any more)" — the global unauthorized
 * handler (errors.ts) owns the redirect; this hook just surfaces the state.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { extendSession, getMe, login, logout, type LoginResult } from "./endpoints";

export const SESSION_QUERY_KEY = ["session"] as const;

/** Identity/role/expiry for the AppShell session slot and role gating. */
export function useSession() {
  return useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: getMe,
    // 401 while signed out is a normal state, not a failure to retry.
    retry: false,
    staleTime: 30_000,
  });
}

/**
 * Sign in; on success the session query refetches and the router navigates.
 *
 * Everything is invalidated, not just the session query: any data fetched
 * while signed out (or under a previous identity) must not survive into the
 * new session — the role can change what the caller is allowed to see.
 */
export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation<LoginResult, Error, { name: string; password: string }>({
    mutationFn: ({ name, password }) => login(name, password),
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}

/**
 * Revoke the session and clear the cookie; the server does both in one call.
 *
 * `clear()` wipes the whole cache: nothing viewed under this identity (agent
 * rows, config bodies) should remain queryable from the devtools after sign
 * out, and a subsequent login as someone else must start from zero.
 */
export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation<{ logged_out: boolean }, Error, void>({
    mutationFn: () => logout(),
    onSuccess: () => {
      queryClient.clear();
    },
  });
}

/**
 * Audited re-auth ("Extend"): fresh credentials, old session revoked, new one
 * minted (DELETE-then-POST — see `extendSession` for why the cookie flow
 * inverts the Streamlit ordering).
 */
export function useExtendSession() {
  const queryClient = useQueryClient();
  return useMutation<LoginResult, Error, { name: string; password: string }>({
    mutationFn: ({ name, password }) => extendSession(name, password),
    // Same rule as useLogin: re-auth can change the role, so nothing cached
    // under the old session may be reused.
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}
