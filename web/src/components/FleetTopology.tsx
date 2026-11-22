import { useId, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { Link } from "@tanstack/react-router";
import type { AgentEntry } from "../api/endpoints";
import { absoluteWhen, heartbeatAgeCompact } from "../lib/heartbeat";
import { useNow } from "../lib/useNow";
import {
  connectorPath,
  MAX_FLEET_NODES,
  nodeKey,
  topologySummary,
} from "../lib/topology";

/**
 * The Overview hero graphic: one server node on the left, the fleet on the
 * right, and a measured line per agent between them.
 *
 * Why measured rather than a fixed SVG: the nodes are real DOM (names, health
 * dots, links, truncation, touch targets) and their sizes change with the text,
 * the viewport and the number of agents. So each render the component reads its
 * own boxes and asks `lib/topology` for the path — the line always meets the
 * node edge, at any width, with no magic numbers.
 *
 * The connecting is the point: a new agent's line draws itself in from the
 * agent's end (agents dial out to the server — dash offset on `pathLength=1`,
 * staggered by `--i`), then a spark keeps travelling along it, agent → server. Both animations are killed by the reduced-motion rule in `index.css`.
 */
export function FleetTopology({
  agents,
  className = "",
}: {
  /** Empty = the zero-agent state: no lines, sockets waiting. */
  agents: AgentEntry[];
  className?: string;
}) {
  const shown = agents.slice(0, MAX_FLEET_NODES);
  const hidden = agents.length - shown.length;
  // Heartbeat recency on every chip, live or not — the graphic should show the
  // conversation, not just that a row exists.
  const now = useNow();
  // Sanitised: `useId` may contain punctuation that breaks `url(#…)`.
  const gradientId = `topology-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;

  const [links, setLinks] = useState<{ id: string; d: string; slot: boolean }[]>([]);
  const [box, setBox] = useState({ w: 0, h: 0 });

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const serverRef = useRef<HTMLDivElement | null>(null);
  const nodes = useRef(new Map<string, HTMLDivElement>());
  const slots = useRef<(HTMLDivElement | null)[]>([null, null, null]);
  // The measure effect is keyed on the node set, not the array identity (which
  // changes on every parent render); the ref hands it the current entries.
  const shownRef = useRef(shown);
  useLayoutEffect(() => {
    shownRef.current = shown;
  });
  const key = `${nodeKey(shown)}::${shown.length === 0 ? "empty" : "fleet"}`;

  useLayoutEffect(() => {
    const wrap = wrapRef.current;
    const server = serverRef.current;
    if (!wrap || !server) return;

    const measure = () => {
      const base = wrap.getBoundingClientRect();
      if (base.width === 0) return; // hidden (drawer closed, tab in background)
      const s = server.getBoundingClientRect();
      const from = { x: s.right - base.left, y: s.top + s.height / 2 - base.top };
      const next: { id: string; d: string; slot: boolean }[] = [];
      const edge = (el: HTMLElement | null) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.left - base.left, y: r.top + r.height / 2 - base.top };
      };
      if (shownRef.current.length === 0) {
        // Zero agents: dashed stubs into the sockets that are waiting for them.
        slots.current.forEach((el, i) => {
          const to = edge(el);
          if (to) next.push({ id: `slot-${i}`, d: connectorPath(from, to), slot: true });
        });
      } else {
        for (const agent of shownRef.current) {
          const to = edge(nodes.current.get(agent.id) ?? null);
          if (to) next.push({ id: agent.id, d: connectorPath(from, to), slot: false });
        }
      }
      setBox({ w: base.width, h: base.height });
      setLinks(next);
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [key]);

  return (
    <div
      className={`card relative flex flex-col justify-between overflow-hidden px-4 py-4 ${className}`}
      role="group"
      aria-label={topologySummary(agents)}
    >
      <div
        ref={wrapRef}
        className="relative flex flex-1 items-center justify-between gap-2 sm:gap-6"
      >
        {box.w > 0 && (
          <svg
            aria-hidden
            className="pointer-events-none absolute inset-0 overflow-visible"
            width={box.w}
            height={box.h}
            viewBox={`0 0 ${box.w} ${box.h}`}
          >
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="0">
                <stop offset="0" style={{ stopColor: "var(--color-accent-bright)" }} stopOpacity="0.9" />
                <stop offset="1" style={{ stopColor: "var(--color-accent)" }} stopOpacity="0.55" />
              </linearGradient>
            </defs>
            {links.map(({ id, d, slot }, i) =>
              slot ? (
                <path
                  key={id}
                  className="topology-slot"
                  d={d}
                  fill="none"
                  stroke="var(--color-border-strong)"
                  strokeWidth="1"
                />
              ) : (
                <g key={id} style={{ "--i": i } as CSSProperties}>
                  <path
                    className="topology-link"
                    d={d}
                    pathLength={1}
                    fill="none"
                    stroke={`url(#${gradientId})`}
                    strokeWidth="1.5"
                  />
                  <path
                    className="topology-spark"
                    d={d}
                    pathLength={1}
                    fill="none"
                    stroke="var(--color-accent-bright)"
                    strokeWidth="2"
                    strokeLinecap="round"
                  />
                </g>
              ),
            )}
          </svg>
        )}

        <ServerNode ref={serverRef} listening={agents.length === 0} />

        <div className="flex min-w-0 max-w-[300px] flex-1 flex-col gap-2">
          {shown.map((agent, i) => (
            <AgentNode
              key={agent.id}
              agent={agent}
              index={i}
              now={now}
              register={(el) => {
                if (el) nodes.current.set(agent.id, el);
                else nodes.current.delete(agent.id);
              }}
            />
          ))}
          {agents.length === 0 && (
            <EmptySockets
              register={(index, el) => {
                slots.current[index] = el;
              }}
            />
          )}
          {hidden > 0 && (
            <p className="pr-1 text-right text-[11.5px] text-[var(--color-muted)]">
              +{hidden} more
            </p>
          )}
        </div>
      </div>

      <p className="mt-3 text-center text-[11.5px] text-[var(--color-muted)]">
        {agents.length === 0
          ? "No agents connected — the server is listening."
          : "Each line is a live OpAMP session."}
      </p>
    </div>
  );
}

const ServerNode = ({
  ref,
  listening,
}: {
  ref: React.Ref<HTMLDivElement>;
  listening: boolean;
}) => (
  <div
    ref={ref}
    className="relative z-10 flex w-[108px] shrink-0 flex-col items-center gap-1.5 rounded-[var(--radius-md)] border border-[var(--color-border-strong)] bg-[var(--color-surface-2)] px-2 py-3 text-center shadow-[var(--inner-highlight)] sm:w-[156px] sm:px-3"
  >
    <span className="relative flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] bg-[var(--color-accent-soft)]">
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="var(--color-accent-strong)"
        strokeWidth="1.3"
        aria-hidden
      >
        <rect x="2.2" y="2.6" width="11.6" height="3.2" rx="1.1" />
        <rect x="2.2" y="6.9" width="11.6" height="3.2" rx="1.1" />
        <rect x="2.2" y="11.2" width="11.6" height="2.2" rx="1.1" />
        <circle cx="4.7" cy="4.2" r="0.55" fill="var(--color-ok)" stroke="none" />
      </svg>
      {listening && (
        <span aria-hidden className="absolute -right-0.5 -top-0.5 flex h-2 w-2">
          <span className="topology-ping absolute inset-0 rounded-full bg-[var(--color-accent-bright)]" />
          <span className="relative h-2 w-2 rounded-full bg-[var(--color-accent-bright)]" />
        </span>
      )}
    </span>
    <span className="text-[12px] font-medium leading-tight text-[var(--color-text)]">
      OpAMP server
    </span>
    <span className="font-mono text-[10px] leading-none text-[var(--color-faint)]">
      /v1/opamp
    </span>
  </div>
);

function AgentNode({
  agent,
  index,
  now,
  register,
}: {
  agent: AgentEntry;
  index: number;
  now: number;
  register: (el: HTMLDivElement | null) => void;
}) {
  const dot =
    agent.healthy === true
      ? "bg-[var(--color-ok)]"
      : agent.healthy === false
        ? "bg-[var(--color-danger)]"
        : "bg-[var(--color-faint)]";
  return (
    <Link
      to="/agents/$agentId"
      params={{ agentId: agent.id }}
      className="topology-node group relative z-10 block rounded-[var(--radius-md)] no-underline"
      style={{ "--i": index } as CSSProperties}
    >
      <div
        ref={register}
        className="flex items-center gap-2 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface)] px-2.5 py-1.5 transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)] group-hover:border-[var(--color-border-strong)] group-hover:bg-[var(--color-surface-2)]"
      >
        <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] font-medium leading-tight text-[var(--color-text)]">
            {agent.name ?? agent.id.slice(0, 8)}
          </span>
          <span className="block truncate text-[10.5px] leading-tight text-[var(--color-muted)]">
            {agent.agent_type}
          </span>
        </span>
        <span
          title={`Last heartbeat ${absoluteWhen(agent.last_heartbeat)}`}
          className="tnum shrink-0 pr-0.5 text-[10px] text-[var(--color-faint)]"
        >
          {heartbeatAgeCompact(agent.last_heartbeat, now)}
        </span>
      </div>
    </Link>
  );
}

/** Three dashed sockets where agents will land — the zero-agent state's "why". */
function EmptySockets({
  register,
}: {
  register: (index: number, el: HTMLDivElement | null) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          ref={(el) => register(i, el)}
          aria-hidden={i > 0}
          className="flex h-[34px] items-center justify-center rounded-[var(--radius-md)] border border-dashed border-[var(--color-border)]"
          style={{ opacity: 1 - i * 0.25 }}
        >
          {i === 0 && (
            <span className="topology-wait text-[10.5px] text-[var(--color-faint)]">
              waiting for agents
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
