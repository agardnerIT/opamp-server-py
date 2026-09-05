#!/usr/bin/env python3
"""
Create GitHub issues from the AI-accessibility plan in ai-access-plan.db.

Source of truth: planning/ai-access-plan.db (approaches, gaps, work_items, decisions).
Creates:
  - one issue per work_item (labeled ai-access + phase-<phase>)
  - one epic issue summarizing the strategy + decisions + child issues

Safety: aborts if any open issue with the `ai-access` label already exists,
unless --force is passed (dup protection for reruns).
Usage: python3 planning/create_github_issues.py [--force] [--dry-run]
"""
import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = "agardnerIT/opamp-server-py"
DB_PATH = Path(__file__).parent / "ai-access-plan.db"
AI_NOTE = (
    "\n---\n"
    "🤖 *This issue was auto-generated from the AI-accessibility plan "
    "(local DB `planning/ai-access-plan.db`) with the assistance of an AI coding agent.*"
)

LABEL_COLORS = {
    "ai-access": "1f6feb",
    "phase-0-api": "bfd4f2",
    "phase-1-client": "8b9dc3",
    "phase-2-cli": "d4c5f9",
    "phase-3-mcp": "c2e0c6",
    "phase-4-skills": "fde2c8",
    "phase-x-cross": "e6e6e6",
}


def run(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, text=True, capture_output=True)


def gh_create_issue(title: str, body: str, labels: list[str]) -> str:
    cmd = ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body-file", "-"]
    for label in labels:
        cmd += ["--label", label]
    proc = run(cmd, input_text=body)
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue create failed for {title!r}:\n{proc.stderr}")
    return proc.stdout.strip()


def issue_number_from_url(url: str) -> int:
    return int(re.search(r"/issues/(\d+)$", url).group(1))


def ensure_labels(conn: sqlite3.Connection, dry_run: bool) -> None:
    existing = set()
    proc = run(["gh", "api", f"repos/{REPO}/labels", "--jq", ".[].name"])
    if proc.returncode == 0:
        existing = set(proc.stdout.split())
    for name, color in LABEL_COLORS.items():
        if name in existing:
            continue
        if dry_run:
            print(f"[dry-run] would ensure label {name!r}")
            continue
        proc = run([
            "gh", "api", "-X", "POST", f"repos/{REPO}/labels",
            "-f", f"name={name}", "-f", f"color={color}",
        ])
        if proc.returncode != 0 and "already_exists" not in proc.stderr:
            print(f"warning: could not create label {name}: {proc.stderr.strip()}")


def build_issue_body(
    item: sqlite3.Row,
    gap_rows: dict[str, tuple[str, str]],
    approaches: dict[str, sqlite3.Row],
) -> str:
    approach = approaches[item["approach"]]
    gaps = [g.strip() for g in item["gaps_addressed"].split(",") if g.strip() and g.strip() != "-"]
    lines = [
        f"## Summary\n{item['description']}",
        f"\n## Approach: {approach['name']}",
        f"{approach['role_in_plan']}",
        f"\n## Gaps addressed",
    ]
    if gaps:
        for gid in gaps:
            title, detail = gap_rows.get(gid, (gid, ""))
            lines.append(f"- **{gid}** {title} — {detail}")
    else:
        lines.append("- None (cross-cutting / project hygiene)")
    lines += [
        "\n## Acceptance criteria",
    ] + [f"- {c.strip()}" for c in item["acceptance"].split(";") if c.strip()]
    lines += [
        f"\n## Metadata",
        f"- Phase: `{item['phase']}`",
        f"- Priority: {item['priority']}",
        f"- Effort: {item['effort']}",
        f"- Status: {item['status']}",
        AI_NOTE,
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="create even if ai-access issues exist")
    parser.add_argument("--dry-run", action="store_true", help="print what would be created")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"plan DB not found: {DB_PATH} (run seed_ai_access_plan.py first)", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    approaches = {r["id"]: r for r in conn.execute("SELECT * FROM approaches")}
    gap_rows = {r["id"]: (r["title"], r["detail"]) for r in conn.execute("SELECT * FROM gaps")}
    items = list(conn.execute("SELECT * FROM work_items ORDER BY phase, priority, id"))
    decisions = list(conn.execute("SELECT topic, decision, reasoning FROM decisions"))

    # Duplicate protection
    proc = run(["gh", "issue", "list", "--repo", REPO, "--label", "ai-access",
                "--state", "open", "--json", "number", "--jq", "length"])
    existing_count = int(proc.stdout.strip() or 0) if proc.returncode == 0 else 0
    if existing_count and not args.force:
        print(f"aborting: {existing_count} open issue(s) already carry the `ai-access` label "
              f"(rerun with --force to create duplicates)", file=sys.stderr)
        return 1

    ensure_labels(conn, dry_run=args.dry_run)

    created: list[tuple[int, str, str]] = []  # (phase, number, title)
    for item in items:
        labels = ["ai-access", f"phase-{item['phase']}"]
        body = build_issue_body(item, gap_rows, approaches)
        if args.dry_run:
            print(f"[dry-run] create: [{', '.join(labels)}] {item['title']}")
            created.append((item["phase"], 0, item["title"]))
            continue
        url = gh_create_issue(item["title"], body, labels)
        num = issue_number_from_url(url)
        created.append((item["phase"], num, item["title"]))
        print(f"created #{num}: {item['title']}")

    # Epic issue
    epic_title = "AI accessibility epic: make opamp-server-py usable by AI LLM agents"
    child_lines = []
    for phase, num, title in created:
        ref = f"[#{num}](https://github.com/{REPO}/issues/{num})" if num else f"(dry-run) {title}"
        child_lines.append(f"- {ref} — {title} ({phase})")
    epic_body = [
        "## Goal",
        "Make the **opamp-server-py** project accessible to AI LLM agents so they can inspect "
        "connected OpAMP agents, run compliance checks, build collector manifests, and manage "
        "alerts — programmatically, reliably, and without hallucinating endpoint shapes.",
        "\n## Strategy: build all four, layered",
        "These are not competing options; each layer wraps the previous one:",
        "1. **Raw REST API + docs** — complete + document the FastAPI surface (foundation; "
        "OpenAPI `/openapi.json` is the self-documenting schema).",
        "2. **Shared `opamp_client` library** — single HTTP/auth/JSON client used by CLI and MCP.",
        "3. **CLI (`opampctl`)** — universal, scriptable, JSON output; the zero-config on-ramp.",
        "4. **MCP server (FastMCP)** — native typed tools for every MCP-capable agent "
        "(`mcp==1.27.2` already installed).",
        "5. **Agent skills** — SKILL.md + AGENTS.md/CLAUDE.md encoding how/when + project gotchas.",
        "\n## Decisions",
    ]
    for d in decisions:
        epic_body.append(f"- **{d['topic']}:** {d['decision']}")
    epic_body += [
        "\n## Sub-issues",
    ] + child_lines
    epic_body += [
        "\n## References",
        "- Plan DB: `planning/ai-access-plan.db` (local, gitignored)",
        "- Seed script: `planning/seed_ai_access_plan.py`",
        f"- Runtime proof of the plan: run `python3 planning/seed_ai_access_plan.py` then "
        f"`sqlite3 planning/ai-access-plan.db 'SELECT * FROM work_items'`",
        AI_NOTE,
    ]

    if args.dry_run:
        print(f"[dry-run] create epic: {epic_title}")
    else:
        url = gh_create_issue(epic_title, "\n".join(epic_body), ["ai-access"])
        print(f"created epic #{issue_number_from_url(url)}: {epic_title}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())