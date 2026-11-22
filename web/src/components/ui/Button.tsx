import type { ButtonHTMLAttributes } from "react";
import { cx } from "./cx";

/**
 * The one button. Four variants stop the per-page invented styles:
 * primary (the single filled action per view — brand indigo fill, white
 * label, 6.9:1), secondary (raised surface), ghost (quiet, for toolbars and
 * table rows), danger (destructive; outline treatment so it never competes
 * with primary for "the filled thing").
 *
 * For TanStack `<Link>`s that need to look like buttons, reuse
 * `buttonStyles({ variant, size })` — the class strings live here so there
 * is still exactly one place to restyle.
 */

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const VARIANT: Record<ButtonVariant, string> = {
  primary:
    "bg-[var(--color-accent-deep)] text-white hover:bg-[var(--color-accent-deep-hover)] disabled:opacity-50",
  secondary:
    "bg-[var(--color-surface-2)] text-[var(--color-text)] border border-[var(--color-border)] hover:bg-[var(--color-surface-3)] hover:border-[var(--color-border-strong)] disabled:opacity-50",
  ghost:
    "text-[var(--color-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] disabled:opacity-40",
  danger:
    "text-[var(--color-danger)] border border-[var(--color-border)] hover:border-[var(--color-danger)] hover:bg-[var(--color-danger-soft)] disabled:opacity-40",
};

const SIZE: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs gap-1.5 rounded-[var(--radius-sm)]",
  md: "h-8 px-3 text-[13px] gap-2 rounded-[var(--radius-sm)]",
  lg: "h-9 px-4 text-sm gap-2 rounded-[var(--radius-md)]",
};

export function buttonStyles(opts?: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
}): string {
  const { variant = "secondary", size = "md", className } = opts ?? {};
  return cx(
    "inline-flex select-none items-center justify-center whitespace-nowrap font-medium",
    "transition-[background-color,border-color,color] duration-[var(--duration-fast)] ease-[var(--ease-out)]",
    VARIANT[variant],
    SIZE[size],
    className,
  );
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return (
    <button
      type={type}
      className={buttonStyles({ variant, size, className })}
      {...rest}
    />
  );
}
