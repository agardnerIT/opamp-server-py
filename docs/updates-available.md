# #46 — "Update available" without turning the server into a beacon

**Status:** plan, nothing implemented.
**Issue:** <https://github.com/agardnerIT/opamp-server-py/issues/46>
**Goal:** tell the operator, in product, that a newer release exists, show how to
upgrade, and never upgrade anything automatically.

The hard part is not the banner. It is that every automatic update check is an
outbound request from someone's infrastructure to a third party. This plan makes
that request **opt-in, lazy, minimal, and indistinguishable between installs**,
and offers an offline path with no third-party contact at all.

---

## 1. What #46 actually needs

1. The server knows its own version.
2. It can learn the latest published version.
3. If latest > current, the UI says so once per session, with a copyable upgrade
   command for the operator's install method.
4. The operator can dismiss it. Nothing installs, restarts, or reconfigures itself.

The issue says "denoted by a new git tag". That is a *source* detail, not a
requirement that the server poll GitHub.

## 2. The privacy problem, stated plainly

A naive implementation (`GET https://api.github.com/...` on a timer, with a
custom User-Agent containing the version) leaks:

| Leak | Who learns it | Why it matters |
|---|---|---|
| Source IP + time of check | GitHub, its CDN, the DNS resolver, any TLS middlebox | A list of organisations running this server, and when their infrastructure is alive |
| Regular check interval | Same | A heartbeat: the server is up even when nobody is looking |
| Current version (if sent) | Same | Which builds are deployed where; who is behind on security fixes |
| Instance ID / installation UUID | Same | Stable cross-time identifier — never do this |
| Browser IPs | GitHub, per admin | Every person who opens the dashboard is also enumerated |
| Fleet/config/agent data | Anyone if the request is ever widened | This server holds collector configs and credentials; no fleet data may ever leave |
| Feed response text | The operator, if rendered as HTML/markdown | A compromised feed becomes a phishing page telling operators to run a command |
| Feed URL with a token | Logs, error messages, screenshots | Private-mirror credentials leak |

The project already has a no-telemetry stance (`.streamlit/config.toml` sets
`gatherUsageStats = false`). #46 must not accidentally introduce the first
phone-home.

## 3. Where the version comes from (there is no free lunch)

An update notifier cannot learn about a release unless **some** process fetches
**some** source. This plan does not pretend otherwise. It decides *which* process
does the fetching and *what* that fetch reveals.

| Topology | Who contacts the source | Server egress | What the source learns |
|---|---|---|---|
| `manual` / `auto` | the server | one anonymous `GET` | an IP and a time; no identity, version, or fleet data |
| `file://` | your existing pipeline / mirror | none | whatever that pipeline already reveals when it pulls the image |
| `off` | nobody | none | nothing until a human reads the release notes |

- **`file://` is a mailbox, not a source.** Nothing about a local file creates
  knowledge. Something you already run — a cron job, a CI step, Argo CD, an
  internal mirror — has to write `update.json` into the server's volume. That
  thing still contacts GitHub. The gain is that it is *your* process, on *your*
  schedule, on a host you already trust with that relationship, and the server
  makes no egress at all. A file baked into the image is useless: it can only
  describe the version you already have.
- **The server asking is not a new third party.** If you deploy from
  `ghcr.io/agardnerit/...`, GitHub already sees your egress IP when you pull the
  image. An update check does not reveal that a deployment exists; it reveals
  that the deployment is *still alive between deploys*. That is the extra signal,
  and it is what the lazy trigger, jitter, and identity-free request minimise.
- **`manual` and `auto` send the same bytes.** The only difference is whether a
  human had to click. `manual` guarantees no unrequested request; `auto` removes
  the click but still fires only while an authenticated human is using the
  product.

So the honest answer to "unless we check GitHub, how do we know?" is: **you
don't.** Either the server asks, or something in your environment that already
talks to GitHub tells the server, or a human reads the release notes. The plan
makes that choice explicit instead of hiding a background poll.

## 4. Design principles

1. **No egress unless a human asked.** No background timer. No check while the
   product is idle. "The server was running" must not be observable remotely.
2. **Send nothing about the instance.** No version, no ID, no hostname, no query
   string, no cookie, no auth, no body. The request is one anonymous `GET` for a
   public file.
3. **Compare locally.** The feed says "0.2.0 exists"; the client decides whether
   that is newer than itself.
4. **One egress point.** The server process makes the request. The browser never
   talks to GitHub automatically; it only follows a link the human clicked.
5. **Never trust the feed for instructions.** Upgrade commands are generated
   locally from a validated semver and a fixed table. No remote text, URLs, or
   shell snippets are rendered.
6. **Fail closed, fail quiet.** A bad/unreachable feed produces "couldn't check",
   not a retry storm, not a scary banner, not a crash.
7. **Local mirror is a first-class option.** Air-gapped and GitOps deployments
   read a file; zero third-party contact.

## 5. Modes (recommended default: `auto`, lazy)

`UPDATE_CHECK` takes one of:

| Mode | Automatic egress | UI | Use case |
|---|---|---|---|
| `off` | Never | Shows current version; explains checks are disabled | Air-gapped, policy-bound |
| `manual` | Never | "Check now" button, labelled with the host it will contact | Normal self-hosted install |
| `auto` **(recommended, default)** | Only when an authenticated human is using the product, at most once per interval | Same as manual, plus automatic refresh | Operators who want the banner without clicking |

`manual` guarantees zero unrequested requests. `off` is for deployments where
even a button is inappropriate. `auto` is deliberately **not** a background
scheduler, and it sends exactly the same bytes as `manual` — the only difference
is whether a human had to click. That is why `auto` can be the default without
weakening the request design: the privacy work is in *what* is sent and *when it
is allowed to fire*, not in forcing a click.

### Why `auto` is lazy

A background task would contact the feed every 24 h forever, including while
nobody is looking. Instead:

- `GET /updates` is a pure read and **never** performs network I/O.
- The UI/CLI, after `GET`, calls `POST /updates/check` only when `auto` is on and
  `now >= next_check_at`.
- `next_check_at = checked_at + interval ± 25%` (default interval 24 h).
- The jitter de-synchronises installs; the lazy trigger means a server nobody
  logs into makes no requests at all.

Trade-off, stated openly: an operator who never opens the UI never gets an
automatic notice. That is the correct trade for this product.

### Optional: `file://` feed (most private automated mode)

`UPDATE_FEED_URL=file:///etc/opamp/update.json` reads a local file populated by
the operator's existing tooling (Ansible, Argo CD, a cron `curl`, an internal
mirror). The server never contacts a third party. The same signature/validation
rules apply.

## 6. Exactly what goes on the wire

Recommended feed: a small static asset attached to the GitHub release.

```http
GET /agardnerIT/opamp-server-py/releases/latest/download/update.json HTTP/1.1
Host: github.com
User-Agent: opamp-server-py-update-check
Accept: application/json
```

That is the whole request. Specifically **not** present:

- no `?version=`, no query string at all;
- no cookies, no `Authorization`, no `.netrc` lookup (`trust_env=False`);
- no W3C `traceparent` (this must not join the operator's tracing pipeline);
- no installation ID, hostname, agent count, or fleet metadata;
- no `If-None-Match`/`If-Modified-Since` (no persistent per-install token);
- a constant User-Agent with no version, so every install is identical.

The only unavoidable disclosure is what any HTTPS fetch reveals: source IP,
time, DNS name, and TLS SNI. Mitigations: default off for automatic checks,
lazy trigger, jitter, local-file option, and a documented "use your egress proxy"
path. Do not invent a bespoke update service or a unique per-install URL — a
shared public asset maximises the anonymity set.

## 7. Feed format and trust

```json
{
  "schema": 1,
  "version": "0.2.0",
  "released_at": "2026-11-10T12:00:00Z",
  "channel": "stable"
}
```

Rules:

- strict schema; unknown fields ignored; no free text, no notes, no URLs;
- `version` must match `^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$` and be ≤ 64 chars;
- `channel` must match the configured `UPDATE_CHANNEL` (default `stable`);
- response capped at 64 KiB and read with a 5 s timeout;
- `https://` only by default; `file://` allowed; redirects refused; link-local /
  private addresses refused unless `UPDATE_ALLOW_PRIVATE_HOSTS=true` (internal
  mirrors);
- the feed URL is a secret if it can contain a token: never log it, never return
  it from the API, redact it in errors.

**Signing (recommended, Phase 2).** Because the notification tells an operator to
run an upgrade command, the feed is a social-engineering target. Sign the
canonical JSON (`sort_keys=True`, `separators=(",", ":")`) with Ed25519
(`cryptography` is already a dependency), publish `update.json` plus a
`signature` field or sibling asset, and verify against a public key baked into
the build. Invalid signature = no notification. `UPDATE_FEED_ALLOW_UNSIGNED=true`
exists only for development and must be surfaced as an insecure posture in the
UI, matching how `server/posture.py` treats other unsafe flags.

If signing is deferred, v1 must fall back to the GitHub tags API and still only
ever render a validated version plus a fixed releases-page link.

## 8. Server surface

New module `server/updates.py` (pure logic, injectable clock + fetcher):

```python
@dataclass(frozen=True)
class UpdateState:
    current: str
    latest: str | None
    update_available: bool
    checked_at: str | None      # ISO-8601 UTC
    next_check_at: str | None
    source: str                 # "none" | "file" | "https"
    mode: str                   # "off" | "manual" | "auto"
    verified: bool | None
    error: str | None           # sanitised, never contains the URL
```

Endpoints:

| Method | Path | Auth | Behaviour |
|---|---|---|---|
| `GET` | `/updates` | none (like `/health`) | Returns cached `UpdateState`; **no network** |
| `POST` | `/updates/check` | admin | Runs exactly one check if the min-interval has elapsed; 429 + `Retry-After` otherwise; returns fresh `UpdateState` |

- `GET` is `Cache-Control: no-store`, contains no feed URL or credentials.
- `POST` is audited (`updates.check`, principal, result) but the audit detail
  contains no URL and no version of the feed.
- A process-wide lock collapses concurrent checks; single-worker uvicorn is the
  documented deployment, so an in-process lock is sufficient.
- No `GET` side effects means crawlers, uptime probes and prefetchers cannot
  cause egress.

## 9. Configuration

All read at call time, like `server/posture.py`.

| Variable | Default | Notes |
|---|---|---|
| `UPDATE_CHECK` | `manual` | `off` / `manual` / `auto` |
| `UPDATE_FEED_URL` | GitHub release asset | Or `file:///...`; may contain a token → secret |
| `UPDATE_FEED_PUBKEY` | baked-in key | Base64 Ed25519 public key; override for forks |
| `UPDATE_FEED_ALLOW_UNSIGNED` | `false` | Dev only; loud posture warning |
| `UPDATE_FEED_TOKEN` | unset | Optional; sent only to the exact configured origin, never logged |
| `UPDATE_INTERVAL_HOURS` | `24` | Auto mode only |
| `UPDATE_JITTER` | `0.25` | ± fraction of the interval |
| `UPDATE_TIMEOUT_SECONDS` | `5` | Single attempt, no retries |
| `UPDATE_MAX_BYTES` | `65536` | Hard cap |
| `UPDATE_CHANNEL` | `stable` | `stable` / `prerelease` |
| `UPDATE_ALLOW_PRIVATE_HOSTS` | `false` | Internal mirrors only |
| `UPDATE_MIN_INTERVAL_SECONDS` | `60` | Anti-beacon floor for `POST` |

Add every one to `.env.sample` with the privacy comment, and to the README
configuration table.

## 10. UI

In `render_sidebar()` (so it appears on every page), for every viewer:

- current version line: `opamp-server-py v0.1.0`;
- if update available: a single `st.warning`:
  "Update available: v0.2.0 (you are on v0.1.0)" with an expander containing
  copyable, locally generated commands:
  - Docker: `docker pull ghcr.io/agardnerit/opamp-server-py-server:0.2.0`
  - pip: `pip install --upgrade opamp-server-py==0.2.0`
  - source: `git fetch --tags && git checkout v0.2.0`
  - a fixed link: `https://github.com/agardnerIT/opamp-server-py/releases`
- if up to date: "Up to date (checked 3 h ago)".
- if `manual`/`off`: "Automatic checks are off. [Check now]" with a caption
  naming the host that will be contacted ("contacts github.com; sends no
  identifying information").
- if the check failed: "Couldn't check for updates" — no retry button loop.

Implementation constraints:

- never render feed text as HTML/markdown; only the validated version string;
- never put the feed URL in the UI;
- cache the state in `st.session_state` for a few minutes so reruns do not hammer
  `/updates`; `POST` only when `auto` and stale;
- unauthenticated viewers can see the notice (it is not fleet data), but only an
  admin/operator can trigger `POST`.

## 11. CLI, client, MCP (all four layers, per AGENTS.md)

- `client/opamp_client.py`: `get_updates()` (read) and `check_updates()`
  (explicit action).
- `opampctl updates status` and `opampctl updates check` (the latter prints a
  one-line "contacting <host>" note unless `--quiet`); JSON by default.
- MCP: `get_update_status` (read-only) and `check_for_updates` (documented as
  causing one outbound request). 29 → 31 tools.
- Tests for each layer, plus one test asserting `get_updates` performs no I/O.

## 12. Close the existing hidden beacon first

`ui/shared.py::get_collector_versions()` currently fetches
`https://api.github.com/repos/open-telemetry/opentelemetry-collector-releases/tags?per_page=250`
with a 1-hour `st.cache_data` TTL, and the Reports page calls it on load. That is
an existing, un-consented, hourly outbound request identifying the deployment —
exactly what #46 is supposed to avoid. Before or with #46:

- route collector-version lookups through the same `server/updates.py` policy
  (one egress point, same mode/consent/caching);
- when `UPDATE_CHECK=off`, serve the last cached list or the static fallback;
- make the Reports page's "refresh versions" click the manual trigger.

Otherwise a server with "update checks off" would still beacon GitHub hourly,
which would make the privacy claim false.

## 13. Logs, metrics, storage

- Log one line per check: `update check: current=0.1.0 latest=0.2.0 verified=true`
  — never the URL, response headers, or body.
- No audit history of checks beyond the single latest result; a table of check
  timestamps is a usage timeline. `POST` gets one audit entry because it is an
  admin action.
- Persist only `{checked_at, next_check_at, latest, channel, verified}` in
  `data/update-check.json` (atomic `os.replace`). No history, no identifiers.
- Optional Prometheus counter `opamp_update_check_total{result}` with no URL or
  version labels. Do not expose `latest` as a metric label.
- Do not add the application version to unauthenticated `/health` (spec version
  is already there; the app version is available from `/updates` and the UI).

## 14. What this deliberately does not build

- no automatic upgrade, restart, `pip install`, `docker pull`, or subprocess;
- no telemetry, usage ping, install count, or "anonymous" UUID;
- no background check loop while idle;
- no browser-side request to GitHub (links are human clicks);
- no remote release notes / HTML / markdown / shell text;
- no project-operated update service that could accumulate install data;
- no webhook registration or install phone-home (that would identify every
  deployment to the project, which is worse than an anonymous GET to GitHub);
- no version or fleet information in the request.

## 15. Tests

Deterministic, offline; inject clock, RNG and an `httpx.MockTransport`.

- `off`/`manual`: zero requests on import, `GET /updates`, and page render.
- `POST /updates/check` makes exactly one request and caches the result.
- min-interval returns 429 + `Retry-After`; no request is made.
- auto + stale triggers once; auto + fresh does not; jitter bounds honoured.
- request assertions: path is the configured feed, method GET, constant UA, no
  query string, no cookie/auth/trace headers, no body, `trust_env` off.
- feed validation: oversized body, bad JSON, wrong schema, bad semver, channel
  mismatch, prerelease vs stable, older/equal version, future `released_at`.
- signature: valid accepted; missing/invalid rejected; unsigned rejected unless
  the dev flag is set (and then reported as insecure).
- SSRF: `http://`, redirect, private IP, `169.254.169.254` refused.
- `file://` feed works and performs no socket I/O.
- output: upgrade commands only contain the validated version; `<script>` in a
  feed field never reaches the DOM; feed URL never appears in the API response,
  logs, or errors.
- auth: anonymous `GET` works; anonymous `POST` 401/403; viewer cannot POST.
- UI: banner appears/hides correctly, no network on rerun, no HTML injection.
- CLI/MCP: status is read-only; check is explicit.
- a socket-level guard fixture that fails any test making an un-mocked outbound
  connection, so the suite proves the default is silent.

## 16. Release plumbing

`release.py` already prints a checklist; extend it so the feed actually exists:

1. write `update.json` (`schema`, version, `released_at`, `channel`);
2. sign it with a key supplied by env (`UPDATE_SIGNING_KEY`, never committed);
3. create a GitHub Release for the tag and attach `update.json` (and signature);
4. print the public-key fingerprint so operators can verify.

A GitHub Actions job on tag push can do the same if the maintainer does not want
to run it locally.

## 17. Docs

- README: "Update notifications (#46)" — modes, what leaves the box, how to
  disable, how to use a local mirror, and how to verify a signature.
- README: a short **Privacy** section: no telemetry; the only outbound requests
  are (a) the configured update feed, (b) OPA if enabled, (c) alert webhooks if
  configured, (d) the tracing exporter if enabled. Defaults for each.
- `.env.sample`: all variables with the privacy comment.
- The UI caption is the policy in one sentence where people actually read it.

## 18. Task breakdown

| # | Task | Notes |
|---|---|---|
| 0 | Route `ui/shared.py` collector-version fetch through the new policy | closes the existing beacon |
| 1 | `server/version.py`: runtime version from package metadata, semver compare | reuse/extend `release.py` parser |
| 2 | `server/updates.py`: modes, fetch policy, validation, signature, cache | pure, injectable |
| 3 | `GET /updates`, `POST /updates/check`, auth, min-interval, audit | no network on GET |
| 4 | `client`, `opampctl`, MCP tools | all four layers |
| 5 | UI banner + manual trigger + copyable upgrade commands | sidebar |
| 6 | `release.py` feed generation/signing + GitHub Release asset | or Actions job |
| 7 | tests, offline guard, docs, `.env.sample`, memory entry | |

## 19. Decisions to confirm

1. **Default mode:** `auto` (recommended: lazy, jittered, anonymous) vs
   `manual` (zero unrequested egress, needs a click) vs `off`.
2. **Auto interval/jitter:** 24 h ± 25% (recommended) vs longer.
3. **Feed source:** signed static `update.json` attached to each release
   (recommended) vs GitHub tags API (no release-process change, unsigned).
4. **Close the existing collector-version fetch as part of #46** (recommended)
   or as a separate privacy issue first.
5. **Feed signing now or in a follow-up.** Recommendation: now — an update
   notice that tells people to run a command is worth authenticating.
