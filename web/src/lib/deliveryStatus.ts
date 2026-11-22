/**
 * The delivery-state vocabulary, exactly as the server defines it (memory.md
 * ~109). The UI never invents states and never invents colors for them:
 *
 * - push responses say  `delivered | queued | unchanged`  (+ reason)
 * - assignment states say `none | queued | delivered | applied | failed |
 *   poisoned | unconfirmed`
 * - `remote_config_status` (the agent's last apply verdict) is the OpAMP enum:
 *   UNSET | APPLYING | APPLIED | FAILED
 *
 * `statusTone()` maps each known state to one of the four token tones; an
 * unknown value renders as `muted` — a state this build does not know is a
 * rendering question, not a new state.
 */

export type StatusTone = "ok" | "warn" | "danger" | "muted";

const TONES: Record<string, StatusTone> = {
  // Applied / delivered: the fleet has the config and it took.
  applied: "ok",
  delivered: "ok",
  completed: "ok",
  ok: "ok",
  // In flight or awaiting proof: queued, applying, waiting for the agent's
  // next report to confirm the hash.
  queued: "warn",
  applying: "warn",
  unconfirmed: "warn",
  // Rollout states (server/rollout.py): running/holding/paused needs a look.
  running: "warn",
  in_flight: "warn",
  paused: "warn",
  // Terminal failures: the agent refused or failed to apply. Poisoned is the
  // server's "stop re-pushing this" verdict — same danger, louder word.
  failed: "danger",
  poisoned: "danger",
  reverted: "danger",
  aborted: "danger",
  // Facts, not problems: nothing pending, nothing changed, nothing known.
  none: "muted",
  unchanged: "muted",
  unset: "muted",
  unknown: "muted",
  pending: "muted",
  cancelled: "muted",
  planned: "muted",
};

/** Tone for a delivery/apply/health status string (case-insensitive). */
export function statusTone(status: string | null | undefined): StatusTone {
  if (!status) return "muted";
  return TONES[status.trim().toLowerCase()] ?? "muted";
}

const LABELS: Record<string, string> = {
  unset: "Unset",
};

/** Human label for a status token; unknown tokens keep their raw spelling. */
export function statusLabel(status: string | null | undefined): string {
  if (!status) return "Unknown";
  const trimmed = status.trim();
  const key = trimmed.toLowerCase();
  if (!TONES[key]) return trimmed; // not a server state — render verbatim
  return LABELS[key] ?? trimmed.toUpperCase();
}
