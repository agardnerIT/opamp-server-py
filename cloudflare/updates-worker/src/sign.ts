import { constantTimeEqual, toHex } from "./validate";

/**
 * Verify `X-Hub-Signature-256` from GitHub.
 *
 * Fails closed: an empty secret, a missing header, or anything other than
 * `sha256=<64 hex>` is a rejection. The comparison is constant-time so a wrong
 * signature does not leak its correct prefix through timing.
 */
export async function verifyGitHubSignature(
  secret: string,
  body: string,
  header: string | null,
): Promise<boolean> {
  if (!secret || !header) return false;
  const match = /^sha256=([0-9a-f]{64})$/i.exec(header.trim());
  if (!match) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(body),
  );
  return constantTimeEqual(toHex(new Uint8Array(signature)), match[1].toLowerCase());
}
