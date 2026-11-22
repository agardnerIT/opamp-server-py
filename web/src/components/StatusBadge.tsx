import { statusLabel, statusTone } from "../lib/deliveryStatus";

/**
 * One consistent rendering for every delivery/apply/health state. The colors
 * come from `statusTone()` (the server's vocabulary → token tone); the dot is
 * aria-hidden and the label carries the meaning, so state is never conveyed
 * by color alone. Pill fill comes from the tone's soft token, the dot from
 * the solid one.
 */
const PILL_CLASS: Record<string, string> = {
  ok: "bg-[var(--color-ok-soft)] text-[var(--color-ok)]",
  warn: "bg-[var(--color-warn-soft)] text-[var(--color-warn)]",
  danger: "bg-[var(--color-danger-soft)] text-[var(--color-danger)]",
  muted: "bg-[var(--color-surface-2)] text-[var(--color-muted)]",
};

const DOT_CLASS: Record<string, string> = {
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
    <span
      className={`inline-flex items-center gap-1.5 rounded-full py-0.5 pr-2 pl-1.5 text-xs font-medium ${PILL_CLASS[tone] ?? PILL_CLASS.muted}`}
    >
      <span
        aria-hidden
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[tone] ?? DOT_CLASS.muted}`}
      />
      {label}
    </span>
  );
}
