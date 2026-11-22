import { statusLabel, statusTone } from "../lib/deliveryStatus";

/**
 * One consistent rendering for every delivery/apply/health state. The colors
 * come from `statusTone()` (the server's vocabulary → token tone); the dot is
 * aria-hidden and the label carries the meaning, so state is never conveyed
 * by color alone.
 */
const TONE_CLASS: Record<string, string> = {
  ok: "bg-[var(--color-ok)]",
  warn: "bg-[var(--color-warn)]",
  danger: "bg-[var(--color-danger)]",
  muted: "bg-[var(--color-muted)]",
};

export function StatusBadge({
  status,
  tone: toneOverride,
  label: labelOverride,
}: {
  status?: string | null;
  tone?: string;
  label?: string;
}) {
  const tone = toneOverride ?? statusTone(status);
  const label = labelOverride ?? statusLabel(status);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${TONE_CLASS[tone] ?? TONE_CLASS.muted}`} />
      {label}
    </span>
  );
}
