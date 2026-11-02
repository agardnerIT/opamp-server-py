# #46 — "Update available"

**Design v2 (simplified).** Two modes. Checking is **on by default**. Optional
email notification through the instance's **own** Resend account. No telemetry,
no installation ID, no central subscriber list.

---

## 1. Two modes

| `UPDATE_CHECK` | Behaviour |
|---|---|
| `on` **(default)** | Once every 24 h the server asks GitHub for the latest release. The UI shows a banner when a newer one exists. |
| `off` | No requests at all. The UI shows a persistent warning. |

**`off` is supported but not recommended.** It is a security risk: an operator
who never sees the notice runs outdated software, including security fixes. The
check is anonymous and GitHub already sees the deployment when the container
image is pulled, so `off` buys very little privacy and costs patch latency.
`off` exists for air-gapped and policy-bound deployments, and the docs say
exactly that rather than presenting it as the "private" choice.

## 2. The request

```http
GET https://api.github.com/repos/agardnerIT/opamp-server-py/releases/latest
Accept: application/vnd.github+json
User-Agent: opamp-server-py-update-check
```

- no current version, no instance ID, no hostname, no query string;
- no cookies, no `Authorization`, no `.netrc`, no proxy auth;
- no W3C `traceparent` (this must not join the operator's tracing pipeline);
- no body; constant User-Agent with no version, so every install looks identical.

**Use the API, not a release asset.** GitHub counts release-asset downloads and
shows them to the repository owner. That is installation telemetry by another
name. The `/releases/latest` API request is not exposed to the maintainer, so
the project learns nothing while GitHub sees only the traffic it already sees
when the image is pulled.

GitHub (and the DNS resolver) see the source IP and the time. That is the
accepted cost of a working, default-on check. The request carries nothing else.

The current version is compared **locally** against `tag_name`; it is never sent.

## 3. Schedule and failure behaviour

- One background task started in the FastAPI lifespan, not a thread per request.
- First check ~60 s after startup, then every 24 h ± a small jitter (±10 %) so
  restarts and fleets do not synchronise.
- Persist `last_checked`; a restart inside the 24 h window waits out the
  remainder instead of checking again. No restart storms.
- 5 s timeout, **one attempt, no retries**, 64 KiB cap, HTTPS only, redirects
  refused.
- Validate `tag_name` as semver (`v` optional, prerelease allowed). Ignore the
  release body, assets, author, and every other field. A 404 means "no stable
  release yet", not an error.
- Any failure is quiet: keep the last known state, log one sanitised line
  (`update check failed: timeout`), never the URL or response body.

## 4. In-product

- Sidebar (all pages): current version, and if newer:
  "Update available: v0.2.0 (you are on v0.1.0)", a fixed link to the releases
  page, and copyable commands generated locally from the validated version:
  - Docker: `docker pull ghcr.io/agardnerit/opamp-server-py-server:0.2.0`
  - pip: `pip install --upgrade opamp-server-py==0.2.0`
  - source: `git fetch --tags && git checkout v0.2.0`
- When `off`: "Update checks are off. **Not recommended** — you may miss
  security fixes. [Why] [Turn on]".
- Only the validated version string is rendered; no remote HTML, markdown, notes
  or shell text. The feed URL is never shown.

Endpoints:

| Method | Path | Auth | Behaviour |
|---|---|---|---|
| `GET` | `/updates` | none, like `/health` | Cached state; **never** network |
| `POST` | `/updates/check` | admin | One immediate check, min-interval 60 s, audited |

`GET` has no side effects, so crawlers, uptime probes and prefetchers cannot
cause egress. `POST` is the only path that can trigger a check on demand.

## 5. Release emails (central, opt-in)

The project runs a consent-based announcement list; the instance does not. The
dashboard embeds a Cloudflare Worker form, and the address goes browser → Worker
→ Resend. The instance stores nothing. Double opt-in, unsubscribe, deletion and
the privacy policy are part of the Worker design.

See [`release-emails.md`](release-emails.md) for the Worker + Resend
architecture, the abuse controls, and the setup checklist.

This is a conscious trade. The project now learns which addresses subscribed and
when — exactly the install data the update check avoids. It is acceptable
because the address is volunteered, double-confirmed, used only for release
announcements, and deletable on request. It is not a usage ping, and the
self-hosted server never sends it.

## 6. Privacy accounting

Leaves the instance:

| Destination | What it sees | When |
|---|---|---|
| GitHub | source IP, time, repo path | once per 24 h, only when `on` |
| Cloudflare Worker | the email, browser IP, Turnstile token | only when an admin submits the form |
| Resend | recipient email, sender, content, delivery events | on confirmation and on each broadcast |

Never leaves the instance automatically: current version, instance ID, fleet
size, agent data, configs, or credentials. The update check is anonymous, and
the email is voluntary and sent by the browser, not by the server.

## 7. Configuration (server side)

| Variable | Default | Notes |
|---|---|---|
| `UPDATE_CHECK` | `on` | `on` / `off`; off is documented as not recommended |
| `UPDATE_CHECK_INTERVAL_HOURS` | `24` | |
| `UPDATE_CHECK_TIMEOUT_SECONDS` | `5` | single attempt, no retries |
| `UPDATE_SUBSCRIBE_URL` | unset | public Worker signup URL; unset hides the email box |

Resend and Cloudflare credentials belong to the Worker, never to the
self-hosted server.

## 8. Remove the existing second beacon

`ui/shared.py::get_collector_versions()` fetches GitHub tags hourly with a 1-hour
`st.cache_data` TTL, and the Reports page calls it on load. With a default-on
daily check this is a second, more frequent outbound path. Fold it into
`server/updates.py` (same 24 h cache, same sanitised request) or drop the remote
lookup and let the operator type the reference collector version. One egress
path, one interval, one privacy story.

## 9. Tests

- default `on`: exactly one request per interval, not per restart or page load;
- `off`: zero requests, warning visible;
- request assertions: method, host, path, constant UA, no query/cookie/auth/
  trace headers, no body;
- semver: `v` prefix, prerelease ordering, older/equal, bad strings, 404, drafts;
- failure: timeout, oversized body, bad JSON, no partial state;
- dashboard: the email box is hidden when `UPDATE_SUBSCRIBE_URL` is unset and
  renders the configured Worker URL when set;
- an autouse fixture sets `UPDATE_CHECK=off` and replaces the transport, so the
  suite can never make a real request;
- UI: banner, off warning, upgrade commands contain only the validated version;
- the Worker has its own Miniflare/Vitest suite (subscribe, confirm, release,
  signatures, idempotency, rate limits, Turnstile, no PII in logs) — see
  `release-emails.md`.

## 10. Task list

| # | Task |
|---|---|
| 1 | `server/version.py` — runtime version from package metadata |
| 2 | `server/updates.py` — fetch, validate, semver compare, cache, state file |
| 3 | daily loop in lifespan + `GET /updates` + `POST /updates/check` |
| 4 | sidebar banner, off warning, copyable commands |
| 5 | Cloudflare Worker + Resend list (see `release-emails.md`) |
| 6 | dashboard iframe/box, gated on `UPDATE_SUBSCRIBE_URL` |
| 7 | client / `opampctl updates` / MCP tools |
| 8 | fold `get_collector_versions()` into the same path |
| 9 | tests, docs, `.env.sample`, README privacy section |

## 11. Decision locked

The email list is **central**: a Cloudflare Worker adds confirmed addresses to
the project's Resend segment, and a GitHub release webhook sends the broadcast.
The self-hosted server stores no address and holds no Resend key. The privacy
cost (the project learns who subscribed) is accepted and written down in
`release-emails.md`.

Open setup questions — Cloudflare hostname/token, Resend segment/domain/key,
GitHub webhook, consent text — are listed in §14 of that document.
