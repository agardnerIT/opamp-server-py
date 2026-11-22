import type { AgentTypeSummary } from "../api/endpoints";

/**
 * Type + provenance badge (#118): what the server believes the agent is, and
 * how it decided. A mis-detection must be visible *before* a config push, not
 * after — hence the source word next to the profile name.
 */
const SOURCE_TEXT: Record<string, string> = {
  pinned: "pinned",
  declared: "declared",
  sniffed: "sniffed",
  fallback: "fallback",
};

export function AgentTypeBadge({
  agentType,
  source,
  profiles,
}: {
  agentType: string;
  source: string;
  profiles?: AgentTypeSummary[];
}) {
  const profile = profiles?.find((p) => p.name === agentType);
  const display = profile?.display_name ?? agentType;
  const icon = profile?.icon;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      {icon && (
        <span aria-hidden className="text-sm leading-none">
          {icon}
        </span>
      )}
      <span className="font-medium">{display}</span>
      <span className="text-[var(--color-muted)]">{SOURCE_TEXT[source] ?? source}</span>
    </span>
  );
}
