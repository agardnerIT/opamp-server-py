# `opampctl` CLI

Every documented API call has a CLI equivalent. **Output is JSON by default**;
use `--raw` to print just the markdown/YAML payload. Install with `pip install -e .`

```bash
# First three commands to try (all safe, read-only):
opampctl health
opampctl agents list
opampctl reports fleet --raw
opampctl config posture                     # config push posture + why each requirement fails

# Agents
opampctl agents list --healthy true --attr environment=prod
opampctl agents list --query "SELECT * FROM agents WHERE host_arch = 'amd64'"
opampctl agents query-schema                # columns + values live agents report
opampctl agents get <agent-id>              # full details incl. metrics
opampctl agents metrics <agent-id>          # latest OTLP metric values
opampctl agents manifest <agent-id> --raw   # OCB manifest.yaml for a slim build
opampctl agents manifest <agent-id> --version 1.2.3
opampctl agents compliance <agent-id>       # OPA evaluation (no-op if OPA disabled)

# Agent credentials (admin; mandatory)
opampctl agents token create <agent-id> --label prod-1   # token (once) + collector snippet
opampctl agents token create <agent-id> --raw            # just the token, for $(...)
opampctl agents token create <agent-id> --retire-previous  # rotate with an immediate cutover
opampctl agents token revoke <agent-id>                  # blocks delivery, keeps agent state

# Remote config (#81) — config push must be enabled and the posture secure
opampctl agents config get <agent-id>        # desired + effective, REDACTED
opampctl agents config get <agent-id> --reveal   # real values: approver role, audited
opampctl agents config set <agent-id> --file collector.yaml --note "add otlp receiver"
opampctl agents config withdraw <agent-id>   # empty config map: run the local config
opampctl config validate --file collector.yaml   # side-effect-free check + hash + verdict
opampctl config apply --file collector.yaml --attr environment=prod --confirm  # admin
opampctl config rotate-key --db data/opamp.db    # re-encrypt under the current key

# Config control plane (#99/#100): propose -> approve -> roll back
opampctl config propose --file collector.yaml --attr environment=prod --note "why"
opampctl config revisions --status proposed      # metadata only, never a body
opampctl config show prop-…                      # one proposal, per-agent verdicts
opampctl config approve prop-… --note "reviewed" # approver: re-resolves + re-validates
opampctl config reject prop-… --reason "wrong tenant"
opampctl config reactivate prop-… --reason "component now installed"
opampctl agents config rollback <agent-id> --note "v2 broke tracing"
opampctl --db data/opamp.db config revisions     # offline: read the store directly

# Staged rollouts (#101)
opampctl config rollout plan prop-…            # dry run: batches, canary, caps
opampctl config rollout create prop-…          # approver: canary released, rest held
opampctl config rollout show <rollout-id>
opampctl config rollout abort|resume|rollback <rollout-id>

# Groups (#111/#132): named SQL selectors
opampctl groups create -n prod-emea --query "SELECT * FROM agents WHERE environment = 'prod'"
opampctl groups list
opampctl groups preview prod-emea              # members now vs last resolution
opampctl groups keys                           # attribute keys live agents report

# Reports
opampctl reports fleet --raw
opampctl reports heavy --threshold 0.8 --raw
opampctl reports outdated --version 0.100.0 --raw

# Compliance
opampctl compliance summary
opampctl compliance policies
opampctl compliance validate
opampctl compliance reload                  # admin
opampctl compliance check <agent-id>        # admin

# Alerts (operator to write; webhook secrets come back redacted on read)
opampctl alerts get
opampctl alerts set @alerts.json            # JSON from file (or inline JSON)
opampctl alerts test --event-type new_agent

# Audit log (#97) — who did what
opampctl audit list --action config. --limit 20    # `action` is a prefix match
opampctl audit list --actor alice --since 2026-10-01
opampctl audit verify                       # exit 1 if the chain is broken
opampctl audit export -f jsonl -o audit.jsonl

# Update checks (#46)
opampctl updates status|check|enable|disable|default

# Admin identity and sessions (#95)
opampctl auth status                        # auth_mode, roles, live principals
opampctl auth whoami                        # your name, role, session expiry
opampctl auth login --name bob --raw        # session token -> export OPAMP_ADMIN_TOKEN
opampctl auth logout                        # revoke this session
opampctl auth sessions list                 # live sessions (admin)
opampctl auth sessions revoke <session-id>  # sign one device out (admin)
opampctl auth principals list               # identities and roles (admin)
opampctl auth principals create bob --role operator --password '…'
opampctl auth principals revoke <principal-id>   # closes their sessions too
```

`--token` / `OPAMP_ADMIN_TOKEN` sends a session token (preferred); `--password` /
`ADMIN_PASSWORD` sends HTTP Basic (legacy, for scripts). When both are set the token
wins.

## Configuration

| Source | Flags | Env vars |
|--------|-------|----------|
| Server URL | `--server`, `-s` | `OPAMP_SERVER_URL` |
| Admin session token | `--token`, `-t` | `OPAMP_ADMIN_TOKEN` (wins over the password) |
| Admin password (legacy Basic) | `--password`, `-p` | `ADMIN_PASSWORD` |

## Offline mode (`--db`)

Read the server's SQLite state file directly — no server needed. Supports the
read-only surface (agents list/get/metrics, manifests, all reports):

```bash
opampctl --db data/opamp.db agents list
opampctl --db data/opamp.db reports fleet --raw
opampctl --db data/opamp.db agents manifest <agent-id> --raw
```

Live-only commands (`health`, `alerts`, `compliance`) exit with a structured
error under `--db`.

## Shell completion

`opampctl` is built with [Typer](https://typer.tiangolo.com/) and ships with
shell completion:

```bash
opampctl --install-completion   # install for the current shell
opampctl --show-completion      # show the install command/instructions
```
