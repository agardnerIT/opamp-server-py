import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { getUpdates, putUpdateSettings, runUpdateCheck } from "../api/admin";
import { useSession } from "../api/session";
import { roleAtLeast } from "../lib/roles";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Switch } from "../components/ui/Switch";

/**
 * Updates (new-ui stage 8): whether this deployment checks for new releases,
 * and running a check. The Streamlit "Release emails" page is folded in here
 * as a note — the signup form lives on the project's update Worker, which the
 * REST API does not expose, so an iframe here would have to hard-code an
 * external URL (the single-origin rule the stage-7 notes set for reports).
 */

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
      <PageHeader
        title="Updates"
        lede="Whether this deployment checks for new releases, and running a check."
      />

      {updates.isPending && <p className="text-sm text-[var(--color-muted)]">Loading…</p>}
      {updates.isError && <EmptyState title="Could not load update state" detail={String(updates.error)} />}

      {state && (
        <div className="card p-4">
          <dl className="flex flex-wrap gap-x-8 gap-y-3">
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-xs text-[var(--color-faint)]">Current</dt>
              <dd className="font-mono text-[13px]">{state.current_version}</dd>
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-xs text-[var(--color-faint)]">Latest</dt>
              <dd className="font-mono text-[13px]">
                {state.latest_tag ?? "unknown"}
                {state.update_available && (
                  <span className="ml-2 font-sans text-xs font-medium text-[var(--color-warn)]">update available</span>
                )}
              </dd>
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-xs text-[var(--color-faint)]">Last checked</dt>
              <dd className="tnum text-[13px]">{state.checked_at?.replace("T", " ").slice(0, 19) ?? "never"}</dd>
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-xs text-[var(--color-faint)]">Automatic checks</dt>
              <dd className="text-[13px]">{state.enabled ? "on" : "off"}</dd>
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
              <Switch
                label="Check for updates automatically"
                checked={state.enabled}
                disabled={!canAdmin}
                onChange={(next) => save.mutate(next)}
              />
              {!canAdmin && (
                <span className="text-xs text-[var(--color-muted)]">
                  (changing this needs the admin role)
                </span>
              )}
              {state.admin_override !== null && canAdmin && (
                <Button
                  size="sm"
                  disabled={save.isPending}
                  onClick={() => save.mutate(null)}
                >
                  Use deployment default
                </Button>
              )}
              <Button
                variant="primary"
                size="sm"
                disabled={!canAdmin || checkNow.isPending || !state.enabled}
                onClick={() => checkNow.mutate()}
              >
                Check now
              </Button>
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

      <Section title="Release emails">
        <p className="max-w-prose text-xs leading-relaxed text-[var(--color-muted)]">
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
      </Section>
    </section>
  );
}
