/**
 * "Last seen" ages — the one way the console renders heartbeat recency.
 *
 * Deliberately *not* a freshness verdict: `is_stale()` lives on the server
 * (`server/state.py`, shared with the reaper and the rollout gate) and its
 * budget is `max(180s, 3 × observed gap)`, so it is not something a browser can
 * or should re-derive. What the browser can do honestly is say how long ago the
 * last heartbeat arrived, in words, for every agent — live ones included.
 *
 * Ages are monotonic and never negative: a heartbeat timestamped in the future
 * (clock skew between agent and browser) reads "just now" rather than "-3s ago".
 */

/** Coarse buckets — the point is "is it talking to me", not precision. */
const JUST_NOW_SECONDS = 5;

/** Parse an ISO timestamp to epoch ms, or null when it is not a date. */
export function heartbeatTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/** Seconds since the heartbeat, floored at 0 (future timestamps clamp). */
export function secondsSince(
  iso: string | null | undefined,
  now: number = Date.now(),
): number | null {
  const ms = heartbeatTime(iso);
  if (ms === null) return null;
  return Math.max(0, Math.round((now - ms) / 1000));
}

/**
 * A sentence fragment for a table cell: `just now`, `12s ago`, `4m ago`,
 * `2h ago`, `3d ago`; beyond a week the date itself is more useful than "62d
 * ago", so it switches to a short date.
 */
export function heartbeatAge(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  const s = secondsSince(iso, now);
  if (s === null) return "—";
  if (s < JUST_NOW_SECONDS) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3_600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `${Math.floor(s / 3_600)}h ago`;
  if (s < 604_800) return `${Math.floor(s / 86_400)}d ago`;
  return new Date(iso as string).toLocaleDateString();
}

/**
 * The same age without the "ago" — for the fleet graphic's chips, where the
 * column is ~40px wide. Unknown ages render as an en dash, not an empty cell.
 */
export function heartbeatAgeCompact(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  const s = secondsSince(iso, now);
  if (s === null) return "—";
  if (s < JUST_NOW_SECONDS) return "now";
  if (s < 60) return `${s}s`;
  if (s < 3_600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3_600)}h`;
  if (s < 604_800) return `${Math.floor(s / 86_400)}d`;
  return new Date(iso as string).toLocaleDateString();
}

/**
 * The full timestamp for a `title`/tooltip, so the relative age never hides the
 * fact. Invalid input is passed through rather than swallowed (same rule as the
 * routes' previous `formatWhen`).
 */
export function absoluteWhen(iso: string | null | undefined): string {
  if (!iso) return "never";
  const ms = heartbeatTime(iso);
  return ms === null ? iso : new Date(ms).toLocaleString();
}
