import type { Env } from "./env";
import {
  deleteFormPage,
  formPage,
  jsonResponse,
  messagePage,
  privacyPage,
} from "./pages";
import {
  contactSegmentIds,
  deleteSubscription,
  ensureContact,
  ResendError,
  resendConfigured,
  sendConfirmation,
  sendDeletionConfirmation,
  sendReleaseBroadcast,
} from "./resend";
import { verifyGitHubSignature } from "./sign";
import { verifyTurnstile } from "./turnstile";
import {
  isValidEmail,
  isValidSemver,
  normalizeEmail,
  randomToken,
  sha256Hex,
} from "./validate";

const PENDING_TTL_SECONDS = 60 * 60 * 24; // 24 h
const DEDUPE_TTL_SECONDS = 60 * 10; // 10 min
const RELEASE_TTL_SECONDS = 60 * 60 * 24 * 30; // 30 days
const MAX_SUBSCRIBE_BYTES = 8 * 1024;
const MAX_WEBHOOK_BYTES = 1024 * 1024;
const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;

const GENERIC_SUBSCRIBE_MESSAGE =
  "If that address can be subscribed, a confirmation email is on its way. " +
  "Check your inbox (and spam). The link expires in 24 hours.";

const GENERIC_DELETE_MESSAGE =
  "If that address is on the list, a deletion email is on its way. " +
  "Check your inbox (and spam). The link expires in 24 hours.";

interface PendingEmail {
  email: string;
  createdAt: string;
}

class HttpError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

function publicBaseUrl(request: Request, env: Env): string {
  return (env.PUBLIC_BASE_URL || new URL(request.url).origin).replace(/\/+$/, "");
}

async function readText(request: Request, maxBytes: number): Promise<string> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > maxBytes) {
    throw new HttpError(413, "That request is too large.");
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).length > maxBytes) {
    throw new HttpError(413, "That request is too large.");
  }
  return text;
}

function wantsJson(request: Request): boolean {
  return (
    (request.headers.get("accept") || "").includes("application/json") ||
    (request.headers.get("content-type") || "").includes("application/json")
  );
}

function errorResponse(
  request: Request,
  env: Env,
  status: number,
  message: string,
): Response {
  if (wantsJson(request)) return jsonResponse({ ok: false, error: message }, status);
  return formPage(env, { error: message, status });
}

function deleteErrorResponse(
  request: Request,
  env: Env,
  status: number,
  message: string,
): Response {
  if (wantsJson(request)) return jsonResponse({ ok: false, error: message }, status);
  return deleteFormPage(env, { error: message, status });
}

interface EmailFields {
  email: string;
  consent: boolean;
  turnstileToken: string;
}

async function parseFields(request: Request): Promise<EmailFields> {
  const contentType = (request.headers.get("content-type") || "").toLowerCase();

  if (contentType.includes("application/json")) {
    const text = await readText(request, MAX_SUBSCRIBE_BYTES);
    let body: Record<string, unknown>;
    try {
      body = JSON.parse(text) as Record<string, unknown>;
    } catch {
      throw new HttpError(400, "That request could not be read.");
    }
    const consent = body.consent;
    const token = body.turnstileToken ?? body["cf-turnstile-response"];
    return {
      email: typeof body.email === "string" ? body.email : "",
      consent: consent === true || consent === "true" || consent === "on",
      turnstileToken: typeof token === "string" ? token : "",
    };
  }

  const text = await readText(request, MAX_SUBSCRIBE_BYTES);
  const params = new URLSearchParams(text);
  const consent = (params.get("consent") || "").toLowerCase();
  return {
    email: params.get("email") || "",
    consent: ["on", "true", "1", "yes"].includes(consent),
    turnstileToken: params.get("cf-turnstile-response") || "",
  };
}

function redirect(location: string): Response {
  return new Response(null, { status: 303, headers: { Location: location } });
}

async function handleSubscribe(request: Request, env: Env): Promise<Response> {
  if (!resendConfigured(env)) {
    console.error(JSON.stringify({ event: "subscribe.not_configured" }));
    return errorResponse(
      request,
      env,
      503,
      "Subscriptions are not configured yet. Please try again later.",
    );
  }

  const input = await parseFields(request);
  const email = normalizeEmail(input.email);

  if (!input.consent) {
    return errorResponse(
      request,
      env,
      400,
      "Please tick the box to confirm you want these emails.",
    );
  }
  if (!isValidEmail(email)) {
    return errorResponse(
      request,
      env,
      400,
      "That email address does not look valid.",
    );
  }
  if (
    !(await verifyTurnstile(
      env,
      input.turnstileToken,
      request.headers.get("CF-Connecting-IP"),
    ))
  ) {
    return errorResponse(
      request,
      env,
      400,
      "The bot check did not pass. Please try again.",
    );
  }

  const emailHash = await sha256Hex(email);
  const dedupeKey = `sent:${emailHash}`;

  // Same response for new, existing and already-subscribed addresses. The
  // dedupe key also stops a repeat submit from bombing one inbox.
  if (await env.UPDATES_KV.get(dedupeKey)) {
    return wantsJson(request)
      ? jsonResponse({ ok: true, message: GENERIC_SUBSCRIBE_MESSAGE })
      : redirect("/?state=sent");
  }

  const token = randomToken();
  const pendingKey = `pending:${token}`;
  await env.UPDATES_KV.put(
    pendingKey,
    JSON.stringify({
      email,
      createdAt: new Date().toISOString(),
    } satisfies PendingEmail),
    { expirationTtl: PENDING_TTL_SECONDS },
  );
  await env.UPDATES_KV.put(dedupeKey, "1", {
    expirationTtl: DEDUPE_TTL_SECONDS,
  });

  try {
    await sendConfirmation(env, email, token, publicBaseUrl(request, env));
  } catch (error) {
    // The address never got a link, so let it retry immediately.
    await env.UPDATES_KV.delete(pendingKey);
    await env.UPDATES_KV.delete(dedupeKey);
    throw error;
  }

  console.log(JSON.stringify({ event: "subscribe.confirmation_sent" }));
  return wantsJson(request)
    ? jsonResponse({ ok: true, message: GENERIC_SUBSCRIBE_MESSAGE })
    : redirect("/?state=sent");
}

async function handleConfirm(url: URL, env: Env): Promise<Response> {
  const token = url.searchParams.get("token") || "";
  if (!TOKEN_RE.test(token)) {
    return messagePage(
      env,
      "Link not valid",
      "This confirmation link is not valid. Request a new one from the form.",
      400,
    );
  }

  const pendingKey = `pending:${token}`;
  const pending = await env.UPDATES_KV.get<PendingEmail>(pendingKey, "json");
  if (!pending) {
    return messagePage(
      env,
      "Link expired",
      "This confirmation link has expired or was already used. " +
        "You can request a new one from the form.",
      410,
    );
  }

  try {
    await ensureContact(env, pending.email);
  } catch (error) {
    console.error(JSON.stringify({ event: "confirm.resend_failed" }));
    return messagePage(
      env,
      "Try again",
      "We could not confirm the subscription right now. " +
        "Please try the link again in a minute.",
      502,
    );
  }

  // Delete after success: a failed Resend call leaves the link usable.
  await env.UPDATES_KV.delete(pendingKey);
  console.log(JSON.stringify({ event: "confirm.subscribed" }));
  return messagePage(
    env,
    "Subscription confirmed",
    "You will get an email when a new opamp-server-py release is published. " +
      "Every email has an unsubscribe link.",
  );
}

async function handleDelete(request: Request, env: Env): Promise<Response> {
  if (!resendConfigured(env)) {
    console.error(JSON.stringify({ event: "delete.not_configured" }));
    return deleteErrorResponse(
      request,
      env,
      503,
      "Deletion is not configured yet. Please try again later.",
    );
  }

  const input = await parseFields(request);
  const email = normalizeEmail(input.email);

  if (!isValidEmail(email)) {
    return deleteErrorResponse(
      request,
      env,
      400,
      "That email address does not look valid.",
    );
  }
  if (
    !(await verifyTurnstile(
      env,
      input.turnstileToken,
      request.headers.get("CF-Connecting-IP"),
    ))
  ) {
    return deleteErrorResponse(
      request,
      env,
      400,
      "The bot check did not pass. Please try again.",
    );
  }

  const dedupeKey = `delete-sent:${await sha256Hex(email)}`;
  if (await env.UPDATES_KV.get(dedupeKey)) {
    return wantsJson(request)
      ? jsonResponse({ ok: true, message: GENERIC_DELETE_MESSAGE })
      : redirect("/delete?state=sent");
  }

  const segments = await contactSegmentIds(env, email);

  // Not on this list: same response as a real member. No email is sent, so an
  // attacker cannot use this endpoint to enumerate subscribers.
  if (segments === null || !segments.includes(env.RESEND_SEGMENT_ID!)) {
    return wantsJson(request)
      ? jsonResponse({ ok: true, message: GENERIC_DELETE_MESSAGE })
      : redirect("/delete?state=sent");
  }

  const token = randomToken();
  const pendingKey = `delete:${token}`;
  await env.UPDATES_KV.put(
    pendingKey,
    JSON.stringify({
      email,
      createdAt: new Date().toISOString(),
    } satisfies PendingEmail),
    { expirationTtl: PENDING_TTL_SECONDS },
  );
  await env.UPDATES_KV.put(dedupeKey, "1", {
    expirationTtl: DEDUPE_TTL_SECONDS,
  });

  try {
    await sendDeletionConfirmation(
      env,
      email,
      token,
      publicBaseUrl(request, env),
    );
  } catch (error) {
    await env.UPDATES_KV.delete(pendingKey);
    await env.UPDATES_KV.delete(dedupeKey);
    throw error;
  }

  console.log(JSON.stringify({ event: "delete.confirmation_sent" }));
  return wantsJson(request)
    ? jsonResponse({ ok: true, message: GENERIC_DELETE_MESSAGE })
    : redirect("/delete?state=sent");
}

async function handleDeleteConfirm(url: URL, env: Env): Promise<Response> {
  const token = url.searchParams.get("token") || "";
  if (!TOKEN_RE.test(token)) {
    return messagePage(
      env,
      "Link not valid",
      "This deletion link is not valid. Request a new one from the delete page.",
      400,
    );
  }

  const pendingKey = `delete:${token}`;
  const pending = await env.UPDATES_KV.get<PendingEmail>(pendingKey, "json");
  if (!pending) {
    return messagePage(
      env,
      "Link expired",
      "This deletion link has expired or was already used. " +
        "You can request a new one from the delete page.",
      410,
    );
  }

  try {
    await deleteSubscription(env, pending.email);
  } catch (error) {
    console.error(JSON.stringify({ event: "delete.resend_failed" }));
    return messagePage(
      env,
      "Try again",
      "We could not complete the deletion right now. " +
        "Please try the link again in a minute.",
      502,
    );
  }

  await env.UPDATES_KV.delete(pendingKey);
  console.log(JSON.stringify({ event: "delete.completed" }));
  return messagePage(
    env,
    "Deleted",
    "Your address has been removed from the opamp-server-py release " +
      "announcement list. If it was not used for anything else in our email " +
      "provider, the contact record was deleted too.",
  );
}

async function handleRelease(request: Request, env: Env): Promise<Response> {
  if (!env.GITHUB_WEBHOOK_SECRET) {
    console.error(JSON.stringify({ event: "release.not_configured" }));
    return jsonResponse({ ok: false, error: "not configured" }, 503);
  }

  const body = await readText(request, MAX_WEBHOOK_BYTES);
  const signature = request.headers.get("X-Hub-Signature-256");
  if (!(await verifyGitHubSignature(env.GITHUB_WEBHOOK_SECRET, body, signature))) {
    return jsonResponse({ ok: false, error: "invalid signature" }, 401);
  }

  if ((request.headers.get("X-GitHub-Event") || "") !== "release") {
    return new Response(null, { status: 204 });
  }

  let payload: {
    action?: string;
    release?: {
      id?: number | string;
      tag_name?: string;
      draft?: boolean;
      prerelease?: boolean;
    };
  };
  try {
    payload = JSON.parse(body) as typeof payload;
  } catch {
    return jsonResponse({ ok: false, error: "invalid payload" }, 400);
  }

  if (payload.action !== "published" && payload.action !== "released") {
    return new Response(null, { status: 204 });
  }

  const release = payload.release;
  if (!release || release.draft === true || release.prerelease === true) {
    return new Response(null, { status: 204 });
  }

  const tag = String(release.tag_name || "");
  if (!isValidSemver(tag)) {
    return jsonResponse({ ok: false, error: "invalid tag" }, 400);
  }
  if (!resendConfigured(env)) {
    return jsonResponse({ ok: false, error: "resend not configured" }, 503);
  }

  // Resend's Idempotency-Key covers /emails, not /broadcasts, so GitHub
  // retries are deduplicated here.
  const releaseKey = `release:${String(release.id ?? tag)}`;
  if (await env.UPDATES_KV.get(releaseKey)) {
    return jsonResponse({ ok: true, duplicate: true });
  }

  try {
    await sendReleaseBroadcast(env, tag);
  } catch (error) {
    console.error(
      JSON.stringify({
        event: "release.broadcast_failed",
        status: error instanceof ResendError ? error.status : undefined,
      }),
    );
    // 5xx so GitHub retries; the KV key is written only after success.
    return jsonResponse({ ok: false, error: "broadcast failed" }, 502);
  }

  await env.UPDATES_KV.put(releaseKey, "1", {
    expirationTtl: RELEASE_TTL_SECONDS,
  });
  console.log(JSON.stringify({ event: "release.broadcast_sent" }));
  return jsonResponse({ ok: true });
}

function handleRoot(env: Env, url: URL): Response {
  if (url.searchParams.get("state") === "sent") {
    return formPage(env, { message: GENERIC_SUBSCRIBE_MESSAGE });
  }
  return formPage(env);
}

function handleDeletePage(env: Env, url: URL): Response {
  if (url.searchParams.get("state") === "sent") {
    return deleteFormPage(env, { message: GENERIC_DELETE_MESSAGE });
  }
  return deleteFormPage(env);
}

function handleHealth(env: Env): Response {
  return jsonResponse({
    ok: true,
    service: "opamp-updates",
    configured: {
      resend: resendConfigured(env),
      github_webhook: Boolean(env.GITHUB_WEBHOOK_SECRET),
      turnstile: Boolean(env.TURNSTILE_SECRET && env.TURNSTILE_SITE_KEY),
    },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const url = new URL(request.url);
      const isRead = request.method === "GET" || request.method === "HEAD";

      if (isRead && url.pathname === "/") {
        return handleRoot(env, url);
      }
      if (request.method === "POST" && url.pathname === "/subscribe") {
        return await handleSubscribe(request, env);
      }
      if (request.method === "GET" && url.pathname === "/confirm") {
        return await handleConfirm(url, env);
      }
      if (isRead && url.pathname === "/privacy") {
        return privacyPage(env);
      }
      if (isRead && url.pathname === "/delete") {
        return handleDeletePage(env, url);
      }
      if (request.method === "POST" && url.pathname === "/delete") {
        return await handleDelete(request, env);
      }
      if (request.method === "GET" && url.pathname === "/delete/confirm") {
        return await handleDeleteConfirm(url, env);
      }
      if (request.method === "POST" && url.pathname === "/release") {
        return await handleRelease(request, env);
      }
      if (isRead && url.pathname === "/health") {
        return handleHealth(env);
      }
      if (isRead && url.pathname === "/robots.txt") {
        return new Response("User-agent: *\nDisallow: /\n", {
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      }
      return new Response("Not found", { status: 404 });
    } catch (error) {
      if (error instanceof HttpError) {
        return errorResponse(request, env, error.status, error.message);
      }
      console.error(
        JSON.stringify({
          event: "worker.error",
          name: error instanceof Error ? error.name : "unknown",
          status: error instanceof ResendError ? error.status : undefined,
        }),
      );
      return errorResponse(
        request,
        env,
        500,
        "Something went wrong. Please try again.",
      );
    }
  },
};
