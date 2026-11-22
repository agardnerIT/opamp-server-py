import { cx } from "./cx";

/**
 * Multi-select checkbox (only where multi-select semantics are real — lists
 * of columns, batch pickers). Native input, brand-colored via
 * `accent-color`, padded label so the hit target is ≥28px. For on/off
 * settings use `Switch` instead.
 */
export function Checkbox({
  checked,
  onChange,
  label,
  disabled,
  className,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: React.ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={cx(
        "inline-flex cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] py-1 pr-2 select-none",
        disabled && "cursor-default opacity-50",
        className,
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className={cx(
          "h-4 w-4 shrink-0 cursor-pointer appearance-none rounded-[4px]",
          "border border-[var(--color-border-strong)] bg-[var(--color-bg)]",
          "checked:border-transparent checked:bg-[var(--color-accent-deep)]",
          "bg-no-repeat bg-center bg-[length:10px_10px]",
          "bg-[url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22 fill=%22none%22 stroke=%22white%22 stroke-width=%222.5%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22><path d=%22M3 8.5l3.5 3.5L13 4.5%22/></svg>')]",
        )}
      />
      <span className="text-[13px]">{label}</span>
    </label>
  );
}
