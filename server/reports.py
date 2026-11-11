"""Fleet report generators (moved from ui/shared.py so the API can serve them).

Pure functions over the `{"agents": [agent.to_dict(), ...]}` shape returned by
`GET /agents` — identical input to what the Streamlit UI passes, so endpoint
payloads match what the UI shows.
"""

from datetime import datetime
from typing import Dict, List


#: Which profile declares each report, and the wording used when one is skipped. The
#: report *names* stay (they are API paths, `opampctl` commands and MCP tools — #117's
#: non-goal), but their scope becomes the profiles that say the question means something.
REPORT_HEAVY = "heavy"
REPORT_OUTDATED = "outdated"


def _profile_of(agent: Dict) -> str:
    """The resolved profile name on an agent dict, or "" when it has none (#120)."""
    return str((agent or {}).get("agent_type") or "")


def split_applicable(agents: List[Dict], report: str) -> tuple:
    """`(applicable, skipped_by_profile)` for one report (#120).

    A report is *about* a question one product can answer. "Which collector has
    components it never uses" is not a question a FluentBit agent can fail; counting it
    as "not heavy" and printing "0" is a measurement where a refusal belongs. The profile
    declares its reports (`reports: [fleet, agent, heavy, outdated]`), so this asks the
    dictionary rather than testing for a product name.
    """
    from server import profiles

    applicable: List[Dict] = []
    skipped: Dict[str, int] = {}
    for agent in agents or []:
        if "agent_type" not in (agent or {}):
            # A payload from before #118, or one a caller built by hand. It carries no
            # claim about what the agent is, so the historical behaviour stands (the
            # report applies) rather than the row silently becoming "not applicable" —
            # absent is not the same evidence as "resolved to something that does not
            # declare this report".
            applicable.append(agent)
            continue
        name = _profile_of(agent)
        profile = profiles.get(name) if name else None
        if profile is not None and report in profile.reports:
            applicable.append(agent)
        else:
            label = name or "unattributed"
            skipped[label] = skipped.get(label, 0) + 1
    return applicable, skipped


def _not_applicable_line(skipped: Dict[str, int]) -> str:
    """The footer that makes a skipped agent visible instead of silently green."""
    if not skipped:
        return ""
    total = sum(skipped.values())
    detail = ", ".join(f"{name} ({count})" for name, count in sorted(skipped.items()))
    return (
        f"\n> Not applicable to {total} agent(s) on profiles: {detail}. "
        "Their profile does not declare this report.\n"
    )


def parse_version(v):
    try:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())
    except (ValueError, AttributeError):
        return (0,)


def _is_heavy(agent, threshold=0.5):
    """Whether one agent is "heavy" — over `threshold` of its components are unused.

    Callers filter by profile first (`split_applicable`): an agent whose profile has no
    component concept is *not applicable*, which is not the same as "not heavy" (#120).
    """
    comps = agent.get("components", {})
    total_count = sum(len(c) for c in comps.values())
    unused_count = sum(1 for cl in comps.values() for c in cl if not c.get("used"))
    return total_count > 0 and (unused_count / total_count) > threshold


def generate_agent_report(data: Dict, format: str = "markdown") -> str:
    agents = data.get("agents", [])

    if format == "markdown":
        lines = ["# Agent Report\n"]
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        lines.append(f"Total Agents: {len(agents)}\n\n")

        if agents:
            lines.append("## Component Versions\n")
            all_versions = {}
            for agent in agents:
                comps = agent.get("components", {})
                for comp_type, comp_list in comps.items():
                    for comp in comp_list:
                        vid = comp.get("version", "unknown")
                        if vid not in all_versions:
                            all_versions[vid] = 0
                        all_versions[vid] += 1

            for version, count in sorted(all_versions.items()):
                lines.append(f"- **{version}**: {count} components\n")

            lines.append("\n## Outdated Collectors\n")
            current_version = "0.149.0"
            outdated = []
            for agent in agents:
                comps = agent.get("components", {})
                versions = set()
                for comp_list in comps.values():
                    for comp in comp_list:
                        vid = comp.get("version", "")
                        if vid:
                            major = vid.split(".")[0] if "." in vid else vid
                            if major.isdigit():
                                versions.add((int(major), vid))
                for major, vid in versions:
                    if major < int(current_version.split(".")[0]):
                        outdated.append((agent.get("id", "")[:16], vid))
                        break

            if outdated:
                for aid, ver in outdated:
                    lines.append(f"- {aid}: {ver}")
            else:
                lines.append("All collectors are up to date.")

            lines.append("\n## Heavy Collectors (Unused Components)\n")
            for agent in agents:
                comps = agent.get("components", {})
                unused_count = 0
                total_count = 0
                for comp_list in comps.values():
                    for comp in comp_list:
                        total_count += 1
                        if not comp.get("used"):
                            unused_count += 1
                if total_count > 0 and unused_count > 0:
                    pct = int((unused_count / total_count) * 100)
                    lines.append(f"- **{agent.get('id', '')[:16]}**: {unused_count}/{total_count} components unused ({pct}%)\n")

            lines.append("\n## Detailed Agent List\n")
            for agent in agents:
                lines.append(f"### {agent.get('id', 'unknown')[:16]}...\n\n")
                lines.append(f"- Healthy: {agent.get('healthy', 'N/A')}\n")
                comps = agent.get("components", {})
                if comps:
                    for comp_type, comp_list in comps.items():
                        in_use = sum(1 for c in comp_list if c.get("used"))
                        lines.append(f"- {comp_type.title()}: {len(comp_list)} total, {in_use} in use\n")
                lines.append("\n")

        return "".join(lines)

    return ""


def generate_heavy_collectors_report(data: Dict, threshold: float = 0.5) -> str:
    agents, skipped = split_applicable(data.get("agents", []), REPORT_HEAVY)

    lines = ["# Heavy Collectors Report\n"]
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"Showing collectors with >{int(threshold * 100)}% unused components\n\n")

    heavy_agents = []
    for agent in agents:
        comps = agent.get("components", {})
        total_count = 0
        unused_count = 0
        for comp_list in comps.values():
            for comp in comp_list:
                total_count += 1
                if not comp.get("used"):
                    unused_count += 1

        if total_count > 0 and (unused_count / total_count) > threshold:
            pct = int((unused_count / total_count) * 100)
            heavy_agents.append((agent.get('id', '')[:16], unused_count, total_count, pct))

    lines.append(f"Found {len(heavy_agents)} heavy collector(s)\n\n")

    if heavy_agents:
        lines.append("## Heavy Collectors\n")
        for aid, unused, total, pct in sorted(heavy_agents, key=lambda x: -x[3]):
            lines.append(f"- **{aid}**: {unused}/{total} unused ({pct}%)\n")

    lines.append(_not_applicable_line(skipped))
    return "".join(lines)


def generate_outdated_collectors_report(data: Dict, latest_version: str = "0.149.0") -> str:
    agents, skipped = split_applicable(data.get("agents", []), REPORT_OUTDATED)
    latest = parse_version(latest_version)

    lines = ["# Outdated Collectors Report\n"]
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"Latest version: v{latest_version}\n\n")

    outdated_agents = []
    for agent in agents:
        comps = agent.get("components", {})
        outdated_comps = []
        for comp_type, comp_list in comps.items():
            for comp in comp_list:
                vid = comp.get("version", "")
                if vid:
                    v = parse_version(vid)
                    if v < latest:
                        outdated_comps.append((comp_type, comp["id"], vid))

        if outdated_comps:
            oldest = min(outdated_comps, key=lambda x: parse_version(x[2]))
            outdated_agents.append((agent.get("id", "")[:16], outdated_comps, oldest[2]))

    lines.append(f"Found {len(outdated_agents)} outdated collector(s)\n\n")

    if outdated_agents:
        lines.append("## Outdated Collectors\n")
        for aid, comps, oldest in sorted(outdated_agents, key=lambda x: parse_version(x[2])):
            lines.append(f"### {aid}\n")
            lines.append(f"- Oldest component: v{oldest}\n")
            lines.append(f"- Outdated components:\n")
            for comp_type, comp_id, ver in comps:
                lines.append(f"  - {comp_type}/{comp_id}: v{ver}\n")
            lines.append("\n")

    lines.append(_not_applicable_line(skipped))
    return "".join(lines)


def _count_outdated_collectors(agents: List[Dict], latest_version: str) -> tuple:
    """Return tuple of (collectors_count, components_count) with outdated versions.

    Only agents whose profile declares the report are counted (#120): the number has to
    mean the same thing as the markdown it is returned beside.
    """
    agents, _skipped = split_applicable(agents, REPORT_OUTDATED)
    latest = parse_version(latest_version)
    collectors_count = 0
    components_count = 0
    for agent in agents:
        has_outdated = False
        comps = agent.get("components", {})
        for comp_list in comps.values():
            for comp in comp_list:
                vid = comp.get("version", "")
                if vid and parse_version(vid) < latest:
                    components_count += 1
                    has_outdated = True
        if has_outdated:
            collectors_count += 1
    return collectors_count, components_count
