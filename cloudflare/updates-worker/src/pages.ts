import type { Env } from "./env";
import { escapeHtml } from "./validate";

const STYLE = `
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: grid; place-items: center;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: Canvas; color: CanvasText;
}
main { width: min(34rem, calc(100% - 2rem)); padding: 2rem 0; }
h1 { font-size: 1.35rem; margin: 0 0 .5rem; }
p { margin: 0 0 1rem; }
label { display: flex; gap: .6rem; align-items: flex-start; margin: 1rem 0; }
input[type=email] {
  width: 100%; padding: .7rem .8rem; font: inherit; border-radius: .45rem;
  border: 1px solid color-mix(in srgb, CanvasText 30%, Canvas); background: Canvas;
  color: CanvasText;
}
button {
  padding: .7rem 1.1rem; font: inherit; font-weight: 600; border: 0;
  border-radius: .45rem; background: #2563eb; color: white; cursor: pointer;
}
button:hover { background: #1d4ed8; }
.note { font-size: .875rem; opacity: .75; }
.message { padding: .8rem 1rem; border-radius: .45rem; margin: 0 0 1rem; }
.message.ok { background: color-mix(in srgb, #16a34a 18%, Canvas); }
.message.error { background: color-mix(in srgb, #dc2626 18%, Canvas); }
a { color: #2563eb; }
`.trim();

export function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
    },
  });
}

function htmlHeaders(): Headers {
  return new Headers({
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    // The form is meant to be embedded in a self-hosted dashboard. It is a
    // public signup form, so embedding anywhere is fine; there is nothing to
    // clickjack beyond "subscribe".
    "Content-Security-Policy":
      "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; " +
      "script-src https://challenges.cloudflare.com; " +
      "frame-src https://challenges.cloudflare.com; " +
      "connect-src 'self' https://challenges.cloudflare.com; " +
      "form-action 'self'; base-uri 'none'; frame-ancestors *",
  });
}

function htmlResponse(body: string, status = 200): Response {
  return new Response(body, { status, headers: htmlHeaders() });
}

function layout(env: Env, title: string, content: string): string {
  const privacy = env.PRIVACY_URL
    ? `<p class="note"><a href="${escapeHtml(env.PRIVACY_URL)}">Privacy policy</a></p>`
    : "";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${STYLE}</style>
</head>
<body>
<main>
${content}
${privacy}
</main>
</body>
</html>`;
}

export function formPage(
  env: Env,
  options: { message?: string; error?: string } = {},
): Response {
  const turnstile = env.TURNSTILE_SITE_KEY
    ? `<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<div class="cf-turnstile" data-sitekey="${escapeHtml(env.TURNSTILE_SITE_KEY)}"></div>`
    : "";

  const banner = options.error
    ? `<p class="message error" role="alert">${escapeHtml(options.error)}</p>`
    : options.message
      ? `<p class="message ok" role="status">${escapeHtml(options.message)}</p>`
      : "";

  const content = `
<h1>opamp-server-py release emails</h1>
<p>Get one email when a new opamp-server-py release is published. Nothing else.</p>
${banner}
<form method="post" action="/subscribe">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" autocomplete="email"
         inputmode="email" maxlength="254" required
         placeholder="you@example.com">
  <label>
    <input type="checkbox" name="consent" value="on" required>
    <span>I agree to receive release announcement emails. I can unsubscribe
    at any time.</span>
  </label>
  ${turnstile}
  <button type="submit">Notify me</button>
</form>
<p class="note">You will receive a confirmation email first. Nothing is added
until you click the link in it.</p>`;
  return htmlResponse(layout(env, "opamp-server-py release emails", content));
}

export function messagePage(
  env: Env,
  title: string,
  message: string,
  status = 200,
): Response {
  const dashboard = env.DASHBOARD_URL
    ? `<p><a href="${escapeHtml(env.DASHBOARD_URL)}">Back to the dashboard</a></p>`
    : "";
  const content = `<h1>${escapeHtml(title)}</h1><p>${message}</p>${dashboard}`;
  return htmlResponse(layout(env, title, content), status);
}
