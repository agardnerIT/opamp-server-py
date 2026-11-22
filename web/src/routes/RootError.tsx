import type { ErrorComponentProps } from "@tanstack/react-router";
import { Link } from "@tanstack/react-router";

/**
 * Root error boundary: a route-level render crash must not blank the app
 * silently. Route errors can be transient (a bad fetch mid-render), so reset
 * is offered before "go home".
 */
export default function RootError({ error, reset }: ErrorComponentProps) {
  const message =
    error instanceof Error ? error.message : String(error ?? "Unknown error");
  return (
    <div className="p-8">
      <h1 className="text-lg font-semibold">Something went wrong</h1>
      <p role="alert" className="mt-2 max-w-prose text-sm text-[var(--color-danger)]">
        {message}
      </p>
      <div className="mt-4 flex gap-3 text-sm">
        {reset && (
          <button
            type="button"
            onClick={() => reset()}
            className="rounded border border-[var(--color-border)] px-3 py-1 hover:text-[var(--color-text)]"
          >
            Try again
          </button>
        )}
        <Link
          to="/"
          className="rounded border border-[var(--color-border)] px-3 py-1 text-[var(--color-accent)] underline"
        >
          Back to Agents
        </Link>
      </div>
    </div>
  );
}
