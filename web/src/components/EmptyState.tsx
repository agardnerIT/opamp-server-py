/**
 * Empty/loading/error state for a list or section — airy where the user is
 * deciding: centered, quiet, and (per the owner's complaint about the
 * compliance page) an optional action slot so "nothing here" still says what
 * to do about it.
 */
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
    <div className="card mt-2 px-6 py-10 text-center">
      <p className="text-[15px] font-medium text-[var(--color-text)]">{title}</p>
      {detail && (
        <p className="mx-auto mt-1.5 max-w-prose text-[13px] leading-relaxed text-[var(--color-muted)]">
          {detail}
        </p>
      )}
      {children && (
        <div className="mt-4 flex justify-center gap-2">{children}</div>
      )}
    </div>
  );
}
