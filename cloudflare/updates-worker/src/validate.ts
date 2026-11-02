const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const SEMVER_RE = /^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const CONTROL_RE = /[\u0000-\u001f\u007f]/;

export function normalizeEmail(raw: string): string {
  return raw.trim().toLowerCase();
}

/**
 * Deliberately permissive. The real proof is the confirmation email, not a
 * regex that tries to implement RFC 5322 and rejects valid addresses.
 */
export function isValidEmail(email: string): boolean {
  return (
    email.length >= 3 &&
    email.length <= 254 &&
    !CONTROL_RE.test(email) &&
    EMAIL_RE.test(email)
  );
}

export function isValidSemver(tag: string): boolean {
  return tag.length > 0 && tag.length <= 64 && SEMVER_RE.test(tag);
}

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => HTML_ESCAPES[char] ?? char);
}

export function toHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(input),
  );
  return toHex(new Uint8Array(digest));
}

/** 32 random bytes, base64url, no padding. 43 characters. */
export function randomToken(bytes = 32): string {
  const raw = new Uint8Array(bytes);
  crypto.getRandomValues(raw);
  let binary = "";
  for (const byte of raw) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let index = 0; index < a.length; index += 1) {
    diff |= a.charCodeAt(index) ^ b.charCodeAt(index);
  }
  return diff === 0;
}
