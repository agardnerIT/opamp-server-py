/**
 * Geometry and copy for the Overview fleet graphic (`components/FleetTopology`).
 *
 * Kept out of the component so the SVG math is unit-tested rather than
 * eyeballed: the component measures its own DOM boxes (a ResizeObserver), hands
 * the two edge midpoints here, and renders whatever path comes back. Nothing in
 * this file touches React or the DOM.
 */

export interface Point {
  x: number;
  y: number;
}

/** Nodes drawn before the graphic collapses the tail into "+N more". */
export const MAX_FLEET_NODES = 5;

/**
 * One connector: server node's right edge midpoint → agent node's left edge
 * midpoint. Horizontal control points keep every curve leaving and entering
 * level, so a fan of them reads as one harness rather than a spider; the bend
 * is clamped so a short gap stays shallow and a wide one cannot bulge out of
 * the card.
 */
export function connectorPath(from: Point, to: Point): string {
  const bend = Math.max(16, Math.min(Math.abs(to.x - from.x) * 0.45, 96));
  const p = (n: number) => Math.round(n * 10) / 10;
  return (
    `M ${p(from.x)} ${p(from.y)} ` +
    `C ${p(from.x + bend)} ${p(from.y)}, ${p(to.x - bend)} ${p(to.y)}, ` +
    `${p(to.x)} ${p(to.y)}`
  );
}

/**
 * The graphic's accessible summary. The nodes' names stay real text in the
 * DOM; this sentence carries the thing a screen reader cannot infer from a
 * list — that the nodes are connected to one server, and how healthy they are.
 */
export function topologySummary(agents: { healthy: boolean | null }[]): string {
  if (agents.length === 0) {
    return "Diagram: the OpAMP server is listening, with no agents connected.";
  }
  const healthy = agents.filter((a) => a.healthy === true).length;
  const total = agents.length;
  return (
    `Diagram: ${total} agent${total === 1 ? "" : "s"} connected to the server, ` +
    `${healthy} reporting healthy.`
  );
}

/** Stable key for the measured node set, so the effect re-measures on change. */
export function nodeKey(agents: { id: string }[]): string {
  return agents.map((a) => a.id).join("|");
}
