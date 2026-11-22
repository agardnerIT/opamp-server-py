import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  getAlerts,
  testAlert,
  updateAlerts,
  type AlertEventConfig,
} from "../api/admin";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Switch } from "../components/ui/Switch";
import { Notice } from "../components/ui/Toast";

/**
 * Alerts (new-ui stage 8). Per-event webhook config. The round-trip is the
 * whole point: GET returns `webhook_url`/`headers` masked with the sentinel,
 * and PUT keeps the stored value wherever the sentinel passes through
 * unchanged — so the fields below render the mask and never scrub it. The
 * body template carries no secret and is shown verbatim.
 *
 * Slimmed from Streamlit: one event at a time (a selector instead of tabs),
 * and "Test" fires against the unsaved form via the /alerts/test
 * `event_config` escape hatch — no implicit autosave.
 */

const HEADERS_PLACEHOLDER = '{"Content-Type": "application/cloudevents+json; charset=UTF-8"}';

const BODY_PLACEHOLDER = JSON.stringify(
  {
    specversion: "1.0",
    type: "io.opentelemetry.opamp.agent.{event_type}",
    source: "opamp-server",
    id: "{id}",
    time: "{time}",
    datacontenttype: "application/json",
    data: { message: "{message}" },
  },
  null,
  2,
);

export default function Alerts() {
  const queryClient = useQueryClient();
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: getAlerts });

  const [event, setEvent] = useState<string | null>(null);
  const [draft, setDraft] = useState<AlertEventConfig | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const events = alerts.data?.events ?? [];
  const effectiveEvent = event ?? events[0] ?? null;

  const configFor = (name: string): AlertEventConfig => {
    const stored = alerts.data?.config.events[name] ?? {};
    return {
      enabled: stored.enabled ?? false,
      webhook_url: stored.webhook_url ?? "",
      headers: stored.headers || HEADERS_PLACEHOLDER,
      body_template: stored.body_template || BODY_PLACEHOLDER,
    };
  };

  // Seed the draft from the server config when the event changes. The draft is
  // the unsaved form; the sentinel rides in `webhook_url`/`headers` verbatim.
  const draftFor = (name: string): AlertEventConfig => {
    if (draft && event === name) return draft;
    return configFor(name);
  };

  const save = useMutation({
    mutationFn: async () => {
      if (!effectiveEvent || !draft) throw new Error("nothing to save");
      await updateAlerts({ [effectiveEvent]: draft });
    },
    onSuccess: () => {
      setMessage({ ok: true, text: "Saved." });
      void queryClient.invalidateQueries({ queryKey: ["alerts"] });
    },
    onError: (err) => setMessage({ ok: false, text: String(err) }),
  });

  const test = useMutation({
    mutationFn: () => {
      if (!effectiveEvent || !draft) throw new Error("nothing to test");
      return testAlert(effectiveEvent, draft);
    },
    onSuccess: (result) =>
      setMessage(
        result.success
          ? { ok: true, text: "Test alert sent." }
          : { ok: false, text: `Test failed: ${result.error ?? "unknown error"}` },
      ),
    onError: (err) => setMessage({ ok: false, text: String(err) }),
  });

  const busy = save.isPending || test.isPending;

  if (alerts.isPending) {
    return <p className="text-sm text-[var(--color-muted)]">Loading alerts config…</p>;
  }
  if (alerts.isError) {
    return (
      <EmptyState
        title="Could not load alerts config"
        detail={String(alerts.error)}
      />
    );
  }

  return (
    <section className="flex flex-col gap-4">
      <PageHeader
        title="Alerts"
        lede="Webhook alerts for fleet events. The webhook URL and headers are returned masked; saving unchanged keeps the stored secret."
      />

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[13px] font-medium">Event</span>
          <select
            value={effectiveEvent ?? ""}
            onChange={(e) => {
              setEvent(e.target.value);
              setDraft(null); // reseed from the server config
              setMessage(null);
            }}
            className="field"
          >
            {events.map((name) => (
              <option key={name} value={name}>
                {name.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
      </div>

      {effectiveEvent &&
        (() => {
          const cfg = draftFor(effectiveEvent);
          const set = (patch: Partial<AlertEventConfig>) => {
            setDraft({ ...cfg, ...patch });
            setMessage(null);
          };
          return (
            <Section title={effectiveEvent.replace(/_/g, " ")} caption="Cloudevents webhook — placeholders: {event_type}, {message}, plus auto-filled {id} and {time}.">
              <div className="flex flex-col gap-3">
                <Switch
                  label="Enabled"
                  checked={cfg.enabled}
                  onChange={(next) => set({ enabled: next })}
                />
                <label className="flex flex-col gap-1">
                  <span className="text-[13px] font-medium">Webhook URL</span>
                  <input
                    type="text"
                    value={cfg.webhook_url}
                    onChange={(e) => set({ webhook_url: e.target.value })}
                    spellCheck={false}
                    className="field"
                    placeholder="https://hooks.example.com/…"
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[13px] font-medium">Headers (JSON)</span>
                  <input
                    type="text"
                    value={cfg.headers}
                    onChange={(e) => set({ headers: e.target.value })}
                    spellCheck={false}
                    className="field"
                    placeholder={HEADERS_PLACEHOLDER}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[13px] font-medium">Body template</span>
                  <textarea
                    rows={8}
                    value={cfg.body_template}
                    onChange={(e) => set({ body_template: e.target.value })}
                    spellCheck={false}
                    className="field"
                    placeholder={BODY_PLACEHOLDER}
                  />
                </label>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="primary"
                    disabled={busy}
                    onClick={() => save.mutate()}
                  >
                    Save
                  </Button>
                  <Button
                    type="button"
                    disabled={busy}
                    onClick={() => test.mutate()}
                  >
                    Send test
                  </Button>
                  {message && (
                    <Notice kind={message.ok ? "ok" : "error"} className="py-1.5">
                      {message.text}
                    </Notice>
                  )}
                </div>
              </div>
            </Section>
          );
        })()}
    </section>
  );
}
