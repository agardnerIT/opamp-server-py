import { Link } from "@tanstack/react-router";

/** Root notFoundComponent: unknown URL, rendered outside the shell. */
export default function NotFound() {
  return (
    <div className="p-8">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <p className="mt-2 text-sm text-[var(--color-muted)]">
        There is no page at this address.
      </p>
      <Link
        to="/"
        className="mt-4 inline-block text-sm text-[var(--color-accent)] underline"
      >
        Back to Agents
      </Link>
    </div>
  );
}
