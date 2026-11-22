/**
 * Placeholder body for a route that exists for navigation parity (new-ui
 * stage 4) but whose real page ships in a later stage: real title, honest
 * "not built yet" state. The later stage replaces this file's content.
 */
export default function StubPage({
  title,
  stage,
  description,
}: {
  title: string;
  /** The redesign stage that builds the real page. */
  stage: number;
  description: string;
}) {
  return (
    <div>
      <h1 className="text-lg font-semibold">{title}</h1>
      <p className="mt-2 max-w-prose text-sm text-[var(--color-muted)]">
        {description}
      </p>
      <p className="mt-4 text-xs text-[var(--color-muted)]">
        Not implemented yet — the real page lands in stage {stage} of the
        redesign.
      </p>
    </div>
  );
}
