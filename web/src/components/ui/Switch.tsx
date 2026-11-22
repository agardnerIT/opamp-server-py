import { cx } from "./cx";

/**
 * Accessible switch for on/off semantics — replaces the tiny native
 * checkboxes the owner called out. `role="switch"` + `aria-checked`; the
 * visible track is 28px tall so the hit target is a finger-sized control,
 * not a precision click.
 *
 * Keyboard: Space toggles (native button behavior); Enter also toggles —
 * both are standard for switch-as-button.
 */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
  className,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** Accessible name; also shown as a label to the right when provided. */
  label: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={cx("inline-flex items-center gap-2.5 select-none", className)}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cx(
          "relative inline-flex h-7 w-11 shrink-0 items-center rounded-full border px-0.5",
          "transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)]",
          checked
            ? "border-transparent bg-[var(--color-accent-deep)]"
            : "border-[var(--color-border-strong)] bg-[var(--color-surface-2)]",
          disabled && "opacity-50",
        )}
      >
        <span
          aria-hidden
          className={cx(
            "inline-block h-5 w-5 rounded-full shadow-sm transition-transform duration-[var(--duration-fast)] ease-[var(--ease-out)]",
            checked
              ? "translate-x-4 bg-white"
              : "translate-x-0 bg-[var(--color-muted)]",
          )}
        />
      </button>
      <span className={cx("text-[13px]", disabled && "opacity-50")}>
        {label}
      </span>
    </label>
  );
}
