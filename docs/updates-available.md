# #46 — In-product update check

**Status:** implemented 2026-11-03. The optional release-email list is a
separate feature; see [`release-emails.md`](release-emails.md).

The server checks GitHub for a newer release once every 24 hours and the UI
shows the current version plus an "update available" banner with copyable
upgrade commands. Nothing is ever downloaded, installed, restarted, or
reconfigured automatically.

---

## 1. Modes and precedence

`UPDATE_CHECK` is the deployer switch:

| Value | Behaviour |
|---|---|
| `on` **(default)** | One anonymous GitHub release check per interval. The admin may turn it off from the UI. |
| `off` | No requests at all. The admin toggle cannot override this. |

`off` is supported but **not recommended**: an operator who never sees the
notice runs outdated software, including security fixes. It exists for
air-gapped or policy-bound deployments.

The admin toggle (Admin → Updates) is persisted in
`data/update-settings.json`. Precedence is:

1. `UPDATE_CHECK=off` → disabled, whatever the admin chose.
2. admin override (`true` / `false`) → wins over the default.
3. otherwise → enabled.

The background loop reads the effective state on every wake-up, so a toggle
needs no restart.

## 2. The request

```http
GET https://api.github.com/repos/agardnerIT/opamp-server-py/releases/latest
Accept: application/vnd.github+json
User-Agent: opamp-server-py-update-check
```

- no current version, instance ID, hostname, or query string;
- no cookies, `Authorization`, `.netrc`, `HTTP(S)_PROXY`, or `traceparent`;
- a constant User-Agent, so every install looks identical;
- 5 s timeout, one attempt, 64 KiB cap, no redirects, HTTPS only;
- `UPDATE_HTTP_PROXY` is the only explicit egress-proxy hook.

The comparison happens locally against `server/version.py`. The tag is not
trusted beyond a strict semver check.

`/releases/latest` is used instead of a release asset on purpose: asset
downloads are counted and visible to the repository owner, while the API
request is not. GitHub still sees the source IP and time; that is the accepted
cost of a working default-on check.

## 3. Schedule and failure behaviour

- Started in the FastAPI lifespan as one task.
- First check ~60 s after startup, then every `UPDATE_CHECK_INTERVAL_HOURS`
  (default 24 h) ± `UPDATE_CHECK_JITTER` (default 10%). The jitter stops
  installs synchronising.
- `next_check_at` is persisted, so a restart inside the window waits instead of
  checking again.
- On failure, keep the last known tag, record a short reason (`timeout`,
  `http:403`, ...), and retry in an hour.
- No retry loop, no backoff storm, no logging of the URL, headers, or body.

## 4. API

| Method | Path | Auth | Behaviour |
|---|---|---|---|
| `GET` | `/updates` | none | Cached state; **never** network |
| `POST` | `/updates/check` | admin | One immediate check; 409 if disabled, 429 + `Retry-After` within 60 s |
| `PUT` | `/updates/settings` | admin | `{"enabled": true/false/null}`; `null` clears the override back to the deployer default |

`GET /updates` returns:

```json
{
  "current_version": "0.1.0",
  "latest_version": "0.2.0",
  "latest_tag": "v0.2.0",
  "update_available": true,
  "checked_at": "2026-11-03T...",
  "next_check_at": "2026-11-04T...",
  "enabled": true,
  "deployment_enabled": true,
  "admin_override": null,
  "source": "github",
  "error": null,
  "releases_url": "https://github.com/agardnerIT/opamp-server-py/releases"
}
```

No feed URL, credentials, or per-install identifier is returned.

## 5. UI

- **Sidebar (every page):** the current version. When a newer release exists, a
  warning links to the release page and an expander contains locally generated
  commands:
  - Docker: `docker pull ghcr.io/agardnerit/opamp-server-py-server:<version>`
  - pip: `pip install --upgrade opamp-server-py==<version>`
  - source: `git fetch --tags && git checkout v<version>`
- **Admin → Updates:** current/latest/last checked, the automatic-check toggle,
  "Check now", and "Use deployment default". The toggle is disabled when the
  deployment has `UPDATE_CHECK=off` or the signed-in role is not `admin`.
- Only the validated version string is rendered; no remote HTML or notes.

## 6. CLI and MCP

- `opampctl updates status|check|enable|disable|default`
- MCP: `get_update_status` (read-only), `check_for_updates` (explicit action),
  and `set_update_check` (only when MCP writes are enabled).

## 7. What is stored

| Path | Contents | Retention |
|---|---|---|
| `data/update-check.json` | `checked_at`, `next_check_at`, `latest_tag`, `last_error` | overwritten every check |
| `data/update-settings.json` | `{"enabled": bool}` admin override | until cleared |

No history of checks, no IP addresses, no hostnames, no fleet data. The only
per-install record is the timestamp of the last check, which is local.

## 8. Tests

`tests/test_updates.py` (offline, fake HTTP client and temporary state):

- exact request shape and constant User-Agent; no version in the request;
- newer/older/prerelease tags; 404 = no stable release; invalid JSON/tag;
- failure keeps the last tag and schedules a retry;
- one check per minute unless forced;
- deployer `off` wins over the admin override; the admin toggle persists and can
  be cleared;
- `GET /updates` never calls the network; `POST` requires admin; 409 when
  disabled; `PUT` toggles.
- Client, CLI, MCP and UI tests cover the new surface.

## 9. Deliberately not built

- no automatic upgrade, download, restart, or subprocess;
- no telemetry, usage ping, install count, or anonymous identifier;
- no version or fleet data in the request;
- no background request while `UPDATE_CHECK=off`;
- no browser-to-GitHub request (the banner is server-side; links are human
  clicks);
- no project-operated update service.

The release-email list is optional and independent: see
[`release-emails.md`](release-emails.md).
