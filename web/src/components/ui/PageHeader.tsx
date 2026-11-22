/**
 * The one page header: title + one-line lede + actions slot. Every route
 * renders through it so the type hierarchy stays identical across pages.
 */
export function PageHeader({
  title,
  lede,
  actions,
  meta,
}: {
  title: string;
  lede?: React.ReactNode;
  /** Right-aligned action buttons. */
  actions?: React.ReactNode;
  /** Small live metadata line (counts, states) under the lede. */
  meta?: React.ReactNode;
}) {
  return (
    <header className="mb-5 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-[-0.01em] text-[var(--color-text)]">
          {title}
        </h1>
        {lede && (
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-[var(--color-muted)]">
            {lede}
          </p>
        )}
        {meta && <div className="mt-2 text-xs text-[var(--color-muted)]">{meta}</div>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
