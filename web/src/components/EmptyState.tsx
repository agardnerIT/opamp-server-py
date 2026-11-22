/** Empty/loading/error state for a list or section, with an optional action. */
export function EmptyState({
  title,
  detail,
  children,
}: {
  title: string;
  detail?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mt-6 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-6 text-sm">
      <p className="font-medium">{title}</p>
      {detail && <p className="mt-1 max-w-prose text-[var(--color-muted)]">{detail}</p>}
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}
