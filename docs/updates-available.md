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

## 5. Email notifications (Resend)

Goal: the admin gets an email when a new version appears, **without the project
learning who they are.**

- Admin-only box in Settings/Updates: one email address, Save, Remove. Masked
  when displayed (`a***@example.com`), never logged.
- On save, send a confirmation email through Resend. Persist only if Resend
  accepts it, which also verifies the address and the instance's configuration.
- On a new version, send **one email per version** (track
  `last_notified_version`). A server restart does not resend.
- Content: current → new version, the fixed releases link, locally generated
  upgrade commands, and an unsubscribe link. Plain text. No tracking pixels, no
  click tracking, no per-recipient Resend tags, no unique links except the
  unsubscribe token.
- The email is sent by **the instance**, using the instance's own
  `RESEND_API_KEY` and a `RESEND_FROM` address on a domain the operator has
  verified in their Resend account. If either is unset, the box is hidden and
  the feature is off.
- Unsubscribe: signed local URL when `PUBLIC_URL` is set, plus a Remove button
  in the UI; either deletes the row. The email is transactional, not marketing,
  but it still gets an unsubscribe path.

What Resend sees: recipient, sender, content, and the server's IP. Document that
next to the box. The project sees nothing.

**Do not ship a shared project Resend key or a project-run mailing list.** If
the instance posted the address to a project endpoint, the project would collect
exactly the install data this design avoids, plus GDPR, unsubscribe and abuse
obligations. If a release newsletter is ever wanted, it belongs on the website
as a separate opt-in, not inside the server.

The email is sent during the instance's daily check. If the instance is down,
there is no email — that is the price of keeping the subscriber list local.

## 6. Privacy accounting

Leaves the instance:

| Destination | What it sees | When |
|---|---|---|
| GitHub | source IP, time, the repo path | once per 24 h, only when `on` |
| Resend | recipient, sender, content, source IP | only if email is configured and a new version appears |

Never leaves: current version, instance ID, fleet size, agent data, configs,
credentials, or the subscriber email to the project. No analytics, no usage
ping, no browser-to-third-party requests (links are human clicks).

## 7. Configuration

| Variable | Default | Notes |
|---|---|---|
| `UPDATE_CHECK` | `on` | `on` / `off`; off is documented as not recommended |
| `UPDATE_CHECK_INTERVAL_HOURS` | `24` | |
| `UPDATE_CHECK_TIMEOUT_SECONDS` | `5` | single attempt, no retries |
| `RESEND_API_KEY` | unset | secret; env only, never in the DB or UI |
| `RESEND_FROM` | unset | verified sender, e.g. `opamp-server <updates@example.com>` |
| `PUBLIC_URL` | unset | used for the unsubscribe link |

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
- email: hidden without Resend config; subscribe sends confirmation; one email
  per version; no tracking parameters; unsubscribe deletes; address masked in
  API and absent from logs;
- an autouse fixture sets `UPDATE_CHECK=off` and replaces the transport, so the
  suite can never make a real request;
- UI: banner, off warning, upgrade commands contain only the validated version.

## 10. Task list

| # | Task |
|---|---|
| 1 | `server/version.py` — runtime version from package metadata |
| 2 | `server/updates.py` — fetch, validate, semver compare, cache, state file |
| 3 | daily loop in lifespan + `GET /updates` + `POST /updates/check` |
| 4 | sidebar banner, off warning, copyable commands |
| 5 | `server/notifications.py` + Resend client + email UI/API |
| 6 | client / `opampctl updates` / MCP tools |
| 7 | fold `get_collector_versions()` into the same path |
| 8 | tests, docs, `.env.sample`, README privacy section |

## 11. One question before implementation

The Resend key: is it **per instance** (each operator configures their own, the
server sends to the admin's address, the project learns nothing — recommended),
or **project-wide** (the server posts addresses to a project service that sends
the mail)? The second turns the project into the data controller for a list of
operators and their addresses, which is the opposite of the "we aren't getting
the install info" goal. If that is what you meant, say so and I will write the
trade-off down explicitly before building it.
