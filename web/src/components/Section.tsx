/**
 * A titled card. Grouping carries structure with space and a surface, not
 * separator lines (better-layout): one card per section of the agent-detail
 * page, generous outer gap, tighter inner one.
 */
export function Section({
  title,
  caption,
  actions,
  children,
}: {
  title: string;
  caption?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="card p-4">
      <header className="mb-3 flex items-baseline justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">{title}</h2>
          {caption && (
            <p className="mt-0.5 text-xs leading-relaxed text-[var(--color-muted)]">
              {caption}
            </p>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

/** Key–value line used inside sections; keys stay muted, values carry content. */
export function KeyValue({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-[var(--color-faint)]">{label}</dt>
      <dd className="min-w-0 text-[13px]">{children}</dd>
    </div>
  );
}
