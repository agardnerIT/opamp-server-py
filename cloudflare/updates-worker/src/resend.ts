import type { Env } from "./env";
import { escapeHtml, isValidSemver, sha256Hex } from "./validate";

export class ResendError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ResendError";
  }
}

export function resendConfigured(env: Env): boolean {
  return Boolean(env.RESEND_API_KEY && env.RESEND_SEGMENT_ID && env.RESEND_FROM);
}

function apiBase(env: Env): string {
  return (env.RESEND_API_BASE || "https://api.resend.com").replace(/\/+$/, "");
}

async function call(
  env: Env,
  path: string,
  init: RequestInit,
  idempotencyKey?: string,
): Promise<unknown> {
  if (!env.RESEND_API_KEY) {
    throw new ResendError(503, "Resend is not configured");
  }

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${env.RESEND_API_KEY}`);
  headers.set("Content-Type", "application/json");
  if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);

  const response = await fetch(`${apiBase(env)}${path}`, { ...init, headers });
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // A non-JSON error body is still an error; fall through to the status.
  }

  if (!response.ok) {
    const body = data as { message?: string; error?: { message?: string } } | null;
    const message = body?.message || body?.error?.message || `Resend ${response.status}`;
    throw new ResendError(response.status, message);
  }
  return data;
}

/**
 * Transactional confirmation email. Uses Resend's Idempotency-Key so a retry
 * within 24h does not send twice.
 */
export async function sendConfirmation(
  env: Env,
  email: string,
  token: string,
  baseUrl: string,
): Promise<void> {
  const link = `${baseUrl}/confirm?token=${encodeURIComponent(token)}`;
  const privacy = env.PRIVACY_URL
    ? `\n\nPrivacy: ${env.PRIVACY_URL}`
    : "";
  const subject = "Confirm your opamp-server-py release emails";

  const text =
    `You asked to receive release announcements for opamp-server-py.\n\n` +
    `Confirm this address:\n${link}\n\n` +
    `The link expires in 24 hours. If you did not request this, ignore this ` +
    `email; nothing was added and you will not receive any more.${privacy}\n`;

  const html =
    `<p>You asked to receive release announcements for opamp-server-py.</p>` +
    `<p><a href="${escapeHtml(link)}">Confirm this address</a></p>` +
    `<p>The link expires in 24 hours. If you did not request this, ignore this ` +
    `email; nothing was added and you will not receive any more.</p>` +
    (env.PRIVACY_URL
      ? `<p><a href="${escapeHtml(env.PRIVACY_URL)}">Privacy</a></p>`
      : "");

  await call(
    env,
    "/emails",
    {
      method: "POST",
      body: JSON.stringify({
        from: env.RESEND_FROM,
        to: [email],
        subject,
        text,
        html,
        ...(env.RESEND_REPLY_TO ? { reply_to: env.RESEND_REPLY_TO } : {}),
      }),
    },
    `confirm-${await sha256Hex(email)}`,
  );
}

/**
 * Add or revive a contact in the one segment this Worker is allowed to write to.
 * Idempotent: an existing contact is patched back to subscribed and re-added to
 * the segment rather than duplicated.
 */
export async function ensureContact(env: Env, email: string): Promise<void> {
  const contactPath = `/contacts/${encodeURIComponent(email)}`;

  try {
    await call(env, contactPath, { method: "GET" });
  } catch (error) {
    if (!(error instanceof ResendError) || error.status !== 404) throw error;
    await call(env, "/contacts", {
      method: "POST",
      body: JSON.stringify({
        email,
        unsubscribed: false,
        segments: [{ id: env.RESEND_SEGMENT_ID }],
      }),
    });
    return;
  }

  await call(env, contactPath, {
    method: "PATCH",
    body: JSON.stringify({ unsubscribed: false }),
  });
  await call(
    env,
    `${contactPath}/segments/${encodeURIComponent(env.RESEND_SEGMENT_ID!)}`,
    { method: "POST" },
  );
}

export function releaseUrl(env: Env, tag: string): string {
  const repo = (env.REPO_URL || "https://github.com/agardnerIT/opamp-server-py").replace(/\/+$/, "");
  return `${repo}/releases/tag/${encodeURIComponent(tag)}`;
}

/**
 * One Broadcast to the dedicated segment, sent immediately. The unsubscribe
 * merge tag is Resend's, not ours: it points at Resend's per-recipient page and
 * keeps the contact's global subscription status correct.
 */
export async function sendReleaseBroadcast(
  env: Env,
  tag: string,
): Promise<void> {
  if (!isValidSemver(tag)) throw new ResendError(400, "invalid release tag");

  const url = releaseUrl(env, tag);
  const subject = `opamp-server-py ${tag} is available`;
  const footer =
    "\n\nYou are receiving this because you confirmed a subscription to " +
    "opamp-server-py release announcements.";

  const text =
    `A new opamp-server-py release is available: ${tag}\n\n` +
    `Release notes: ${url}${footer}\n\n` +
    `Unsubscribe: {{{RESEND_UNSUBSCRIBE_URL}}}\n`;

  const html =
    `<p>A new opamp-server-py release is available: <strong>${escapeHtml(tag)}</strong></p>` +
    `<p><a href="${escapeHtml(url)}">Release notes</a></p>` +
    `<p>You are receiving this because you confirmed a subscription to ` +
    `opamp-server-py release announcements.</p>` +
    `<p><a href="{{{RESEND_UNSUBSCRIBE_URL}}}">Unsubscribe</a></p>`;

  await call(env, "/broadcasts", {
    method: "POST",
    body: JSON.stringify({
      segment_id: env.RESEND_SEGMENT_ID,
      from: env.RESEND_FROM,
      ...(env.RESEND_REPLY_TO ? { reply_to: env.RESEND_REPLY_TO } : {}),
      subject,
      text,
      html,
      send: true,
    }),
  });
}
