"""Fleet report generators (moved from ui/shared.py so the API can serve them).

Pure functions over the `{"agents": [agent.to_dict(), ...]}` shape returned by
`GET /agents` — identical input to what the Streamlit UI passes, so endpoint
payloads match what the UI shows.
"""

from datetime import datetime
from typing import Dict, List


def parse_version(v):
    try:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts if p.isdigit())
    except (ValueError, AttributeError):
        return (0,)


def _is_heavy(agent, threshold=0.5):
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
    agents = data.get("agents", [])

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

    return "".join(lines)


def generate_outdated_collectors_report(data: Dict, latest_version: str = "0.149.0") -> str:
    agents = data.get("agents", [])
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

    return "".join(lines)


def _count_outdated_collectors(agents: List[Dict], latest_version: str) -> tuple:
    """Return tuple of (collectors_count, components_count) with outdated versions."""
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
