import type { Env } from "./env";

/**
 * Server-side Turnstile verification. Disabled only when TURNSTILE_SECRET is
 * unset, which is the local-development path; production sets both the secret
 * and the public site key.
 */
export async function verifyTurnstile(
  env: Env,
  token: string,
  remoteIp: string | null,
): Promise<boolean> {
  if (!env.TURNSTILE_SECRET) return true;
  if (!token) return false;

  const body = new URLSearchParams({
    secret: env.TURNSTILE_SECRET,
    response: token,
  });
  if (remoteIp) body.set("remoteip", remoteIp);

  const response = await fetch(
    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    { method: "POST", body },
  );
  if (!response.ok) return false;

  const result = (await response.json()) as { success?: boolean };
  return result.success === true;
}
