import { useId } from "react";
import { cx } from "./cx";

/**
 * The (i) affordance: dependency-free tooltip on hover/focus, positioned
 * above the trigger. The trigger is a real button (keyboard reachable), the
 * popup carries `role="tooltip"` via aria-describedby. For longer content —
 * multi-step how-tos, unmet-requirement detail — pass `modal` content
 * through `Modal` instead; this component is for one or two sentences.
 */
export function InfoHint({
  text,
  label = "What does this mean?",
  className,
}: {
  text: string;
  label?: string;
  className?: string;
}) {
  const id = useId();
  return (
    <span className={cx("group relative inline-flex", className)}>
      <button
        type="button"
        aria-label={label}
        aria-describedby={id}
        className="inline-flex h-7 w-7 items-center justify-center rounded-full text-[var(--color-faint)] transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] hover:text-[var(--color-text)]"
      >
        <svg
          aria-hidden
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
        >
          <circle cx="8" cy="8" r="6.6" />
          <path d="M8 7.2v3.4" strokeLinecap="round" />
          <circle cx="8" cy="5" r="0.7" fill="currentColor" stroke="none" />
        </svg>
      </button>
      <span
        id={id}
        role="tooltip"
        className={cx(
          "invisible absolute bottom-full left-1/2 z-30 mb-1.5 w-64 -translate-x-1/2",
          "card p-2.5 text-xs leading-relaxed text-[var(--color-text)] shadow-[var(--shadow-overlay)]",
          "group-hover:visible group-focus-within:visible",
        )}
      >
        {text}
      </span>
    </span>
  );
}
