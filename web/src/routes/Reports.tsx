import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getAgentTypes, listAgents } from "../api/endpoints";
import {
  getFleetReport,
  getHeavyCollectorsReport,
  getOutdatedCollectorsReport,
  type FleetReport,
  type HeavyCollectorsReport,
  type OutdatedCollectorsReport,
} from "../api/reports";
import { ApiError } from "../api/errors";
import { EmptyState } from "../components/EmptyState";
import { Markdown } from "../components/Markdown";
import { REPORT_LABELS, offeredReports, type ReportKind } from "../lib/reportTypes";

/**
 * Reports (new-ui stage 7). Read-only: the server generates the markdown
 * (`server/reports.py`), this page renders it and offers the download. Which
 * reports are offered is the union of what the fleet's profiles declare — a
 * fleet whose agents have no component concept is never offered a "Heavy
 * agents" option that would report on a concept it does not have (#121).
 */

type ReportData = FleetReport | HeavyCollectorsReport | OutdatedCollectorsReport;

/** Caption under the selector: what this report is about, with the server's counts. */
function reportCaption(kind: ReportKind, data: ReportData): string {
  const skipped = (r: HeavyCollectorsReport | OutdatedCollectorsReport) =>
    Object.entries(r.not_applicable ?? {})
      .map(([profile, n]) => `${n} × ${profile}`)
      .join(", ");
  switch (kind) {
    case "fleet":
      return `Full fleet analysis — ${(data as FleetReport).agent_count} agent(s)`;
    case "heavy": {
      const r = data as HeavyCollectorsReport;
      return `Agents with >50% unused components — ${r.heavy_count} found` +
        (skipped(r) ? ` · not applicable: ${skipped(r)}` : "");
    }
    case "outdated": {
      const r = data as OutdatedCollectorsReport;
      return `${r.collectors_count} agent(s) with components below v${r.version} ` +
        `(${r.components_count} component(s) found)` +
        (skipped(r) ? ` · not applicable: ${skipped(r)}` : "");
    }
  }
}

function download(markdown: string, filename: string) {
  const blob = new Blob([markdown], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function Reports() {
  const [kind, setKind] = useState<ReportKind>("fleet");
  const [version, setVersion] = useState("0.149.0");

  // Which reports exist for *this* fleet comes from the profiles its agents
  // resolve to; the agents list decides whether the page is useful at all.
  const types = useQuery({ queryKey: ["agent-types"], queryFn: getAgentTypes, staleTime: 5 * 60_000 });
  const agents = useQuery({ queryKey: ["agents", {}], queryFn: () => listAgents() });

  const offered = offeredReports(types.data?.types);
  const effectiveKind: ReportKind = offered.includes(kind) ? kind : "fleet";

  const report = useQuery({
    queryKey: ["report", effectiveKind, effectiveKind === "outdated" ? version : null],
    queryFn: async (): Promise<ReportData> => {
      if (effectiveKind === "fleet") return getFleetReport();
      if (effectiveKind === "heavy") return getHeavyCollectorsReport();
      return getOutdatedCollectorsReport(version);
    },
    enabled: (agents.data?.count ?? 0) > 0,
  });

  const data = report.data;

  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Reports</h1>
        {agents.data && (
          <span className="text-xs text-[var(--color-muted)]">
            {agents.data.count} connected
          </span>
        )}
      </div>

      {(agents.data?.count ?? 0) === 0 && (
        <EmptyState
          title="No agents connected"
          detail="Connect agents to generate reports. The reports describe the live fleet — there is nothing to summarise yet."
        />
      )}

      {(agents.data?.count ?? 0) > 0 && (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Report type</span>
              <select
                value={effectiveKind}
                onChange={(e) => setKind(e.target.value as ReportKind)}
                className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm"
              >
                {offered.map((k) => (
                  <option key={k} value={k}>
                    {REPORT_LABELS[k]}
                  </option>
                ))}
              </select>
            </label>
            {effectiveKind === "outdated" && (
              <label className="flex flex-col gap-1 text-xs">
                <span className="font-medium">Minimum version</span>
                <input
                  type="text"
                  value={version}
                  onChange={(e) => setVersion(e.target.value)}
                  spellCheck={false}
                  className="w-36 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 font-mono text-sm"
                  placeholder="0.149.0"
                  aria-describedby="outdated-version-hint"
                />
              </label>
            )}
            {data && (
              <button
                type="button"
                onClick={() =>
                  download(data.report_markdown, `${REPORT_LABELS[effectiveKind].toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}.md`)
                }
                className="rounded-md border border-[var(--color-border)] px-2 py-1.5 text-xs hover:text-[var(--color-text)]"
              >
                Download Markdown
              </button>
            )}
          </div>

          {effectiveKind === "outdated" && (
            <p id="outdated-version-hint" className="text-[11px] text-[var(--color-muted)]">
              Reference collector version (plain semver, e.g. 0.149.0); components older than this are flagged.
            </p>
          )}

          {data && (
            <p className="text-xs text-[var(--color-muted)]">
              {reportCaption(effectiveKind, data)}
            </p>
          )}

          {report.isPending && (
            <p className="text-sm text-[var(--color-muted)]">Generating report…</p>
          )}

          {report.isError && (
            <EmptyState
              title={
                report.error instanceof ApiError && report.error.kind === "client"
                  ? "Report refused"
                  : "Could not generate report"
              }
              detail={report.error instanceof ApiError ? report.error.message : String(report.error)}
            >
              <button
                onClick={() => report.refetch()}
                className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs hover:text-[var(--color-text)]"
              >
                Retry
              </button>
            </EmptyState>
          )}

          {data && (
            <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
              <Markdown text={data.report_markdown} />
            </div>
          )}
        </>
      )}
    </section>
  );
}
