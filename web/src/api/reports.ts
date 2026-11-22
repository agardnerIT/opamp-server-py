/**
 * Typed wrappers for the report endpoints (new-ui stage 7). The server
 * generates the markdown itself (`server/reports.py`) — the UI renders and
 * offers it for download, never regenerates or reinterprets it.
 *
 * The payloads are `Dict[str, Any]` in FastAPI (generated type `unknown`), so
 * the narrow interfaces below mirror `server/main.py`'s return dicts.
 */

import { api } from "./client";
import { unwrap } from "./errors";

/** `GET /reports/fleet` — full fleet summary. */
export interface FleetReport {
  report_markdown: string;
  agent_count: number;
}

/** `GET /reports/heavy-collectors?threshold=` — collectors with many unused components. */
export interface HeavyCollectorsReport {
  report_markdown: string;
  heavy_count: number;
  threshold: number;
  applicable_count: number;
  /** Agents per profile the report does not cover (#120) — "not heavy" ≠ "clean". */
  not_applicable: Record<string, number>;
}

/** `GET /reports/outdated-collectors?version=` — components older than the reference. */
export interface OutdatedCollectorsReport {
  report_markdown: string;
  collectors_count: number;
  components_count: number;
  version: string;
  applicable_count: number;
  not_applicable: Record<string, number>;
}

export async function getFleetReport(): Promise<FleetReport> {
  return unwrap<FleetReport>(api.GET("/reports/fleet"));
}

export async function getHeavyCollectorsReport(
  threshold?: number,
): Promise<HeavyCollectorsReport> {
  return unwrap<HeavyCollectorsReport>(
    api.GET("/reports/heavy-collectors", { params: { query: { threshold } } }),
  );
}

export async function getOutdatedCollectorsReport(
  version?: string,
): Promise<OutdatedCollectorsReport> {
  return unwrap<OutdatedCollectorsReport>(
    api.GET("/reports/outdated-collectors", { params: { query: { version } } }),
  );
}
