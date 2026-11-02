export interface Env {
  UPDATES_KV: KVNamespace;

  /** Resend. All three are required before /subscribe or /release will work. */
  RESEND_API_KEY?: string;
  RESEND_SEGMENT_ID?: string;
  RESEND_FROM?: string;
  RESEND_REPLY_TO?: string;

  /** Test seam; defaults to https://api.resend.com. */
  RESEND_API_BASE?: string;

  /** GitHub release webhook secret. Required for /release. */
  GITHUB_WEBHOOK_SECRET?: string;

  /** Turnstile. Set both to enable; omitted in local development. */
  TURNSTILE_SITE_KEY?: string;
  TURNSTILE_SECRET?: string;

  /** Public URL of this Worker, used in confirmation links. */
  PUBLIC_BASE_URL?: string;
  PRIVACY_URL?: string;
  DASHBOARD_URL?: string;
  REPO_URL?: string;
}
