# AI Accessibility Work Plan (Local SQLite)

SQLite plan DB for making **opamp-server-py** accessible to AI LLM agents.

- **DB:** `planning/ai-access-plan.db` (gitignored via `*.db`, local-only)
- **Seed script:** `planning/seed_ai_access_plan.py` (reproducible — rerun to rebuild)

## Schema

| Table | Purpose |
|-------|---------|
| `approaches` | The 4 candidate strategies (CLI / MCP / API+docs / skills) + why each is useful for AI agents |
| `gaps` | AI-accessibility gaps found in the codebase (G1–G9), with file references |
| `work_items` | 17 phased tasks, each mapped to an approach + gaps it addresses |
| `decisions` | Recorded trade-off decisions with reasoning (strategy, shared client, auth, transports…) |

## Useful queries

```sql
-- Full plan by phase
SELECT phase, id, priority, effort, approach, title
FROM work_items
ORDER BY phase, priority, id;

-- Why each approach was chosen (rationale)
SELECT id, name, why_useful, role_in_plan FROM approaches;

-- Everything needing to be done before the CLI/MCP can exist is phase 0-1
SELECT title FROM work_items
WHERE phase IN ('0-api', '1-client') ORDER BY priority;

-- Gaps each work item closes
SELECT w.title, w.gaps_addressed
FROM work_items w WHERE w.gaps_addressed != '-';

-- Decisions log
SELECT topic, decision, reasoning FROM decisions;
```

## Summary

**Recommendation: build all four — layered** (API+docs → shared client → CLI → MCP server → skills),
because they are not competing options: the API is the trust substrate, the client prevents
duplicated logic, the CLI is universal and cheap, the MCP server gives best agent ergonomics
where supported (`mcp==1.27.2` already installed), and skills encode the how/when knowledge
from `memory.md` that no schema can convey.

See the full explanation and rationale in the `approaches` and `decisions` tables.