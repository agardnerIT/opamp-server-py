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
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <header className="mb-3 flex items-baseline justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold">{title}</h2>
          {caption && <p className="mt-0.5 text-xs text-[var(--color-muted)]">{caption}</p>}
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
      <dt className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  );
}
