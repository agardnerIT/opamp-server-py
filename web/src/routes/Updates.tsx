import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { getUpdates, putUpdateSettings, runUpdateCheck } from "../api/admin";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";

/**
 * Updates (new-ui stage 8): whether this deployment checks for new releases,
 * and running a check. The Streamlit "Release emails" page is folded in here
 * as a note — the signup form lives on the project's update Worker, which the
 * REST API does not expose, so an iframe here would have to hard-code an
 * external URL (the single-origin rule the stage-7 notes set for reports).
 */

const buttonClass =
  "rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:text-[var(--color-text)] disabled:opacity-40 disabled:hover:text-inherit";

export default function Updates() {
  const session = useSession();
  const canAdmin = roleAtLeast(session.data?.role, "admin");
  const queryClient = useQueryClient();

  const updates = useQuery({ queryKey: ["updates"], queryFn: getUpdates });
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["updates"] });

  const save = useMutation({
    mutationFn: (enabled: boolean | null) => putUpdateSettings(enabled),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(String(err)),
  });

  const checkNow = useMutation({
    mutationFn: runUpdateCheck,
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    // 409 = checks disabled, 429 = ran less than a minute ago — surface them.
    onError: (err) => setError(String(err)),
  });

  const state = updates.data;
  const disabled = state ? !state.deployment_enabled : false;
  const disabledError =
    error !== null && /409/.test(error) ? "Update checks are disabled." : null;
  const rateError =
    error !== null && /429/.test(error)
      ? "A check ran less than a minute ago. Try again shortly."
      : null;

  return (
    <section className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Updates</h1>
        <p className="mt-1 max-w-prose text-xs text-[var(--color-muted)]">
          Whether this deployment checks for new releases, and running a check.
        </p>
      </div>

      {updates.isPending && <p className="text-sm text-[var(--color-muted)]">Loading…</p>}
      {updates.isError && <EmptyState title="Could not load update state" detail={String(updates.error)} />}

      {state && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
          <dl className="flex flex-wrap gap-x-8 gap-y-3 text-sm">
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Current</dt>
              <dd className="font-mono">{state.current_version}</dd>
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Latest</dt>
              <dd className="font-mono">
                {state.latest_tag ?? "unknown"}
                {state.update_available && (
                  <span className="ml-2 text-[var(--color-warn)]">update available</span>
                )}
              </dd>
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Last checked</dt>
              <dd>{state.checked_at?.replace("T", " ").slice(0, 19) ?? "never"}</dd>
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Automatic checks</dt>
              <dd>{state.enabled ? "on" : "off"}</dd>
            </div>
          </dl>
          {state.error && (
            <p role="status" className="mt-3 text-xs text-[var(--color-warn)]">
              The last check failed: {state.error}
            </p>
          )}
          {disabled && (
            <p className="mt-3 text-xs text-[var(--color-muted)]">
              Automatic update checks are disabled by the deployment
              (<code>UPDATE_CHECK=off</code>). The admin toggle cannot override that.
            </p>
          )}

          {!disabled && (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={state.enabled}
                  disabled={!canAdmin}
                  onChange={(e) => save.mutate(e.target.checked)}
                />
                Check for updates automatically
              </label>
              {!canAdmin && (
                <span className="text-xs text-[var(--color-muted)]">
                  (changing this needs the admin role)
                </span>
              )}
              {state.admin_override !== null && canAdmin && (
                <button
                  type="button"
                  className={buttonClass}
                  disabled={save.isPending}
                  onClick={() => save.mutate(null)}
                >
                  Use deployment default
                </button>
              )}
              <button
                type="button"
                className={buttonClass}
                disabled={!canAdmin || checkNow.isPending || !state.enabled}
                onClick={() => checkNow.mutate()}
              >
                Check now
              </button>
            </div>
          )}
          {(disabledError || rateError) && (
            <p role="status" className="mt-2 text-xs text-[var(--color-warn)]">
              {disabledError ?? rateError}
            </p>
          )}
          {error && !disabledError && !rateError && (
            <p role="alert" className="mt-2 text-xs text-[var(--color-danger)]">
              {error}
            </p>
          )}
        </div>
      )}

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h2 className="text-sm font-semibold">Release emails</h2>
        <p className="mt-1 max-w-prose text-xs text-[var(--color-muted)]">
          Get one email when a new release is published — the address is stored
          by the project's signup service, not by this server, so it is not
          managed from here.{" "}
          {state?.releases_url && (
            <>
              Releases are listed at{" "}
              <a
                href={state.releases_url}
                target="_blank"
                rel="noreferrer"
                className="underline hover:text-[var(--color-text)]"
              >
                GitHub releases
              </a>
              .
            </>
          )}
        </p>
      </div>
    </section>
  );
}
