# Release emails — Cloudflare Worker + Resend

**Status:** plan.
**Decision:** the project runs a central, consent-based release announcement list.
The self-hosted server stores no email address. The update *check* (see
`updates-available.md`) is unchanged; this is the email add-on.

This replaces the earlier "per-instance Resend key" idea. That was more private,
but it required every operator to own a Resend account. The trade is explicit:
the project becomes the data controller for a list of email addresses.

---

## Current state (2026-11-02)

- Worker deployed: `https://opamp-updates.tame-lamp8409.workers.dev`
- KV namespace `UPDATES_KV` created; pending/dedupe/release-id keys are live.
- `/health` reports `resend: false`, `github_webhook: false`, `turnstile: false`.
- Admin page has a "Release emails" tab, gated on `UPDATE_SUBSCRIBE_URL` (empty
  by default, so the box is hidden).
- Privacy policy at `/privacy` and a two-step deletion flow at `/delete` are
  implemented; the policy is a draft in `src/privacy.ts`.
- 38 offline Worker tests pass; CI runs `npm test` and `npm run typecheck`.
- Still needed: Resend domain/segment/from/key, GitHub webhook secret, a test
  address, and confirmation of the privacy contact. Turnstile is optional but
  recommended.

---

## 1. Shape

```
 self-hosted dashboard
   (iframe / "notify me" box)
            │
            ▼
   Cloudflare Worker  ◄──── GitHub  release: published  (webhook)
      │        │
      │        └──────────► Resend  Contacts → Segment → Broadcast
      │
      └──► KV: pending confirmations, dedupe, release idempotency
```

- The Worker is the **only** holder of the Resend API key.
- GitHub holds only the webhook secret; the Resend key never goes into Actions.
- The self-hosted server holds only the public Worker URL (for the iframe).
- Resend broadcasts to a dedicated **Segment**, with Resend's per-recipient
  unsubscribe link.

## 2. Components

| Component | Purpose |
|---|---|
| Worker `opamp-updates` | `/subscribe`, `/confirm`, `/release`, `/health`, signup form |
| KV `UPDATES_KV` | short-lived pending tokens, per-email dedupe, release idempotency |
| Turnstile (recommended) | stops scripted signup / confirmation-email bombing |
| Resend Contacts | one contact per confirmed address |
| Resend Segment | `opamp-server-py release announcements` — the only send target |
| Resend Broadcasts | the release email, with `{{{RESEND_UNSUBSCRIBE_URL}}}` |
| GitHub webhook | `release` event, `published` action |
| Dashboard iframe | the in-product box; no PII touches the instance |

## 3. Flows

### 3.1 Subscribe (double opt-in)

```
POST /subscribe  { email, turnstileToken? }
```

1. Validate the address (trim, lowercase, length ≤ 254, one `@`, no control
   characters). Do not try to fully validate RFC 5322.
2. Verify Turnstile if configured (`siteverify`, secret in the Worker).
3. Check `sent:<sha256(email)>` in KV. If present, return the **same generic
   success response** — no "already subscribed", no enumeration.
4. Generate a 32-byte random token. `PUT pending:<token>` =
   `{email, ts}` with a 24 h TTL. `PUT sent:<sha256(email)>` with a 10 min TTL.
5. Send a **transactional** confirmation email via Resend
   `POST /emails`, with `Idempotency-Key: confirm/<sha256(email)>`:
   - from `RESEND_FROM`, subject "Confirm your opamp-server-py release emails";
   - link `https://<worker>/confirm?token=<token>`;
   - "If you didn't request this, ignore this email. Nothing was added."
6. Always return: *"If that address can be subscribed, a confirmation email is
   on its way."* Never say whether it already exists.

### 3.2 Confirm

```
GET /confirm?token=<token>
```

1. `GET pending:<token>`. Missing/expired → "link expired or already used".
2. Delete the pending token.
3. Upsert the Resend contact:
   - `POST /contacts` with `{email, unsubscribed: false, segments: [{id: SEGMENT_ID}]}`;
   - if the contact already exists, `PATCH /contacts/<email>` with
     `{unsubscribed: false}` and `POST /contacts/<email>/segments`.
4. Render a small success page with a link back to the dashboard.
5. No email is added before this step, so an unconfirmed address never consumes
   a contact slot and never receives a broadcast.

### 3.3 Release → Broadcast

GitHub webhook: `release`, action `published`, JSON payload.

```
POST /release
X-Hub-Signature-256: sha256=...
```

1. Verify the HMAC with `GITHUB_WEBHOOK_SECRET` (constant-time compare).
2. Ignore `draft: true`, `prerelease: true`, and actions other than `published`.
3. `release.id` (or `tag_name`) is the idempotency key in KV for 30 days.
   Resend supports `Idempotency-Key` for `/emails`, **not** for `/broadcasts`,
   so this check is ours.
4. `POST https://api.resend.com/broadcasts`:

```json
{
  "segment_id": "<RESEND_SEGMENT_ID>",
  "from": "opamp-server-py <updates@...>",
  "subject": "opamp-server-py v0.2.0 is available",
  "text": "... version, fixed release URL, unsubscribe placeholder ...",
  "html": "... same, escaped ...",
  "send": true
}
```

5. Include `{{{RESEND_UNSUBSCRIBE_URL}}}` in both bodies. That is Resend's
   per-recipient unsubscribe page; it flips the contact's global subscription
   status. Do not build our own unsubscribe flow.
6. The subject uses the validated `tag_name`. The body is a **fixed template**
   with the release URL; the GitHub release body is not rendered as HTML.
   (Plain-text notes in a `<pre>` block are fine if wanted; markdown-to-HTML in a
   Worker is not worth the injection surface.)
7. Return 2xx quickly. On a Resend error, return 5xx so GitHub retries; the KV
   key is only written after a successful create, or written first and cleared
   on failure — decide in implementation (prefer "write after success").

### 3.4 Unsubscribe

Resend's `{{{RESEND_UNSUBSCRIBE_URL}}}` points at Resend's unsubscribe page. The
contact is marked unsubscribed globally; Resend will not send future broadcasts.
Unsubscribed contacts are **not deleted** automatically. Add a periodic manual
step (or a small scheduled Worker) to delete unsubscribed contacts after N days,
and document that a deletion request is handled by deleting the contact in
Resend.

### 3.5 Delete my data

Double opt-in, like subscribing. `POST /delete` accepts an address, checks that
it is in the release segment, and sends a confirmation email only if it is. The
response is identical either way, so the endpoint cannot be used to enumerate
subscribers. `GET /delete/confirm?token=` then:

1. removes the address from **this** segment;
2. deletes the global Resend contact only if no other segment still uses it, so
   a Resend account shared with another project is not damaged;
3. consumes the token.

The email address is in the KV token for 24 h, then it expires. A `privacy@`
mailbox remains for access/correction requests and for anyone who cannot use the
form.

### 3.6 Privacy policy

`GET /privacy` serves the policy from `src/privacy.ts`. It covers only this list:
what is collected (address, confirmation time, subscription status), why,
consent, processors (Resend, Cloudflare), retention, unsubscribe, deletion,
rights, and the no-tracking statement. The controller name and contact are
configurable (`PRIVACY_CONTACT`); it is a draft for the owner to review.

A data processing agreement is a legal document between the controller and
Resend/Cloudflare, not an app page. The policy links their privacy policies; use
their standard DPAs if one is required.

## 4. Worker API

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/` | public | signup form (HTML), consent checkbox, Turnstile |
| `POST` | `/subscribe` | public | JSON or form-encoded; generic response |
| `GET` | `/confirm` | token | confirms, adds contact, success page |
| `POST` | `/release` | GitHub HMAC | creates and sends the broadcast |
| `GET` | `/health` | public | `{ok, configured}` — no secrets |
| `GET` | `/unsubscribe` | — | not needed; Resend hosts it |

Error responses are deliberately vague to avoid email enumeration:
`{"ok": true}` for every accepted subscribe, and a generic message for bad
tokens. Log the real reason server-side, without the address or IP.

## 5. Data

| Where | What | Retention |
|---|---|---|
| Worker KV `pending:<token>` | email + timestamp | 24 h TTL |
| Worker KV `sent:<hash>` | SHA-256 of the address | 10 min TTL |
| Worker KV `release:<id>` | release id | 30 days |
| Worker KV `delete:<token>` | email + timestamp, unconfirmed deletion | 24 h |
| Worker KV `delete-sent:<hash>` | SHA-256 of the address | 10 min |
| Resend contact | email, subscribed status, created_at, properties | until deleted |
| Resend events | sends, deliveries, bounces, complaints, opens/clicks | plan-dependent |

Not stored: names, hostnames, instance IDs, version, fleet data, or IP
addresses in our own KV. Cloudflare request logs still contain IPs; set the
Worker's observability retention deliberately and say so in the privacy policy.

**Turn off open and click tracking** for this broadcast unless there is a
reason not to. They turn an announcement into per-recipient behaviour data.

## 6. Abuse and security

A public endpoint that causes email to be sent is an email-bombing tool unless
it is constrained.

- **Double opt-in is the primary control.** Nothing is added until the recipient
  clicks the link in their own inbox.
- **Turnstile** on the form, verified server-side. Use the test keys in local
  development.
- **Cloudflare rate limiting** on `/subscribe` (e.g. 10/min/IP) and `/confirm`
  (e.g. 30/min/IP); add a global Worker-side cap per minute.
- **Per-address dedupe** (`sent:<hash>`, 10 min) so repeat submits cannot bomb
  one inbox.
- **No enumeration**: identical response and timing for new, existing, and
  already-subscribed addresses.
- **GitHub HMAC** verification; reject unsigned or stale deliveries.
- **Broadcast idempotency** by release id; GitHub retries must not double-send.
- **Resend key handling.** Resend only offers `full_access` or
  `sending_access`; contacts and broadcasts require `full_access`, so the Worker
  key is powerful and there is no narrower one. Store it with
  `wrangler secret put`, never in the repo, the dashboard, or logs; treat the
  Worker code as security-critical and rotate the key if it is ever exposed.
- **No secrets in the self-hosted server**: only the public Worker URL.
- Escape the version string; validate it against
  `^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$`; never render the release body as HTML.
- Send only to the dedicated segment; never to "all contacts".
- Keep a test recipient and use Resend's test mode before the first real send.

## 7. Cloudflare setup

- Worker: TypeScript module, `wrangler.jsonc`, in `cloudflare/updates-worker/`.
- KV namespace `UPDATES_KV`.
- Route: `updates.<domain>/*` (custom domain) or `*.workers.dev` for a first
  dry run. A custom domain looks better in the confirmation email and avoids
  sharing a `workers.dev` host with other projects.
- Secrets: `RESEND_API_KEY`, `RESEND_SEGMENT_ID`, `RESEND_FROM`,
  `GITHUB_WEBHOOK_SECRET`, `TURNSTILE_SECRET` (if enabled).
- Vars: `PUBLIC_BASE_URL`, `PRIVACY_URL`, `DASHBOARD_URL`, `ALLOWED_ORIGIN`
  (only if the form is posted cross-origin rather than iframed).
- Turnstile widget for the Worker hostname and `localhost`.
- WAF rate-limiting rules for `/subscribe` and `/confirm`.
- Observability: Workers Logs on, but do not log email addresses or raw IPs.
- Deploy: `npx wrangler deploy`, then `wrangler tail` for the first test.

## 8. Resend setup

Resend renamed **Audiences → Segments**; the API field is now `segment_id`.
Broadcasts are sent to a segment, not to an audience.

- Verify the sending domain (SPF, DKIM, DMARC). Prefer a subdomain such as
  `updates.<domain>` so release mail cannot damage the main domain's reputation.
- Create a segment, e.g. `opamp-server-py release announcements`; record its ID.
- Create an API key with `full_access` (contacts and broadcasts are not
  available to `sending_access` keys). Store it as a Worker secret.
- Confirm Broadcasts are available on the plan and check the contact quota.
- Set the `from` name/address and a monitored `reply_to`.
- Configure the unsubscribe page branding if wanted; the default Resend page is
  fine.
- Disable open/click tracking.
- Optional: Resend webhooks for `email.bounced` and `email.complained` to a
  Worker endpoint, so a bad send is visible. Monitor the Resend dashboard at
  minimum.
- Add a `List-Unsubscribe` / `List-Unsubscribe-Post` header if Resend does not
  add one for broadcasts; the merge tag covers the visible link.

## 9. GitHub setup

- Repo → Settings → Webhooks → Add webhook.
  - Payload URL: `https://updates.<domain>/release`
  - Content type: `application/json`
  - Secret: the same `GITHUB_WEBHOOK_SECRET`
  - Event: **Releases** only.
- The release must be **published** (not a draft) and **not** a prerelease.
  `release.py` currently pushes tags; make sure a GitHub Release is created for
  every tag, or the webhook never fires.
- GitHub retries failed deliveries; the release-id KV guard makes that safe.
- No GitHub Actions secrets are needed. If a workflow is preferred instead of a
  webhook, it calls the Worker with a bearer secret and the same idempotency
  rule; the Worker still holds the Resend key.

## 10. In-product box

Two options, in order of preference:

1. **Iframe the Worker's signup form** (`st.components.v1.iframe`). The
   dashboard passes only `UPDATE_SUBSCRIBE_URL`. The email goes browser →
   Worker, never through the self-hosted server. The Worker form owns the
   consent text, Turnstile, success and error states.
2. **Server-side POST** from the Streamlit form. Simpler to test, but the
   operator's server becomes the sender and the Worker sees the instance's IP.
   It also needs CORS if done from the browser.

Either way:

- show the box only to an authenticated admin;
- an unchecked consent checkbox, with a link to the privacy policy;
- state that Resend sends the mail and that the address is stored by the
  project, not by the local instance;
- no pre-filled address, no hidden field with instance metadata.

## 11. Legal / privacy checklist

Collecting addresses makes the project the data controller. Minimum:

- privacy policy covering: what is collected (email), why (release
  announcements), lawful basis (consent), processors (Resend, Cloudflare),
  retention, deletion, and how to withdraw consent;
- double opt-in record: the Resend contact's `created_at` is the confirmation
  time; keep the confirmation email copy as evidence;
- unsubscribe in every broadcast (Resend merge tag);
- a deletion route (`privacy@`) and a documented process;
- no open/click tracking, or disclose it if enabled;
- sender identity and a physical postal address in the footer (CAN-SPAM);
- DPAs with Resend and Cloudflare if required in your jurisdiction;
- do not add custom properties such as instance ID, version, or hostname;
- keep the list separate from any other Resend contacts.

## 12. Tests

- Miniflare/Vitest against the Worker with a mocked `fetch` for Resend:
  - subscribe → generic 200, pending token written, one confirmation email;
  - duplicate subscribe → no second email within the dedupe window;
  - confirm → contact created with the segment, token deleted, replay is safe;
  - confirm with a bad/expired token → friendly page, no contact;
  - release webhook → signature verified, draft/prerelease skipped, one
    broadcast, replay does not double-send;
  - bad HMAC → 401, no broadcast;
  - Turnstile failure → no email;
  - rate limit → 429, no email;
  - broadcast body contains `{{{RESEND_UNSUBSCRIBE_URL}}}` and the validated tag;
  - no email/IP in logs.
- Dashboard: the iframe renders only when `UPDATE_SUBSCRIBE_URL` is set and the
  admin is authenticated.

## 13. Deployment order

1. Resend: domain verified, segment created, key issued, tracking off.
2. Cloudflare: KV + Worker deployed with secrets, `/health` green.
3. Worker: subscribe + confirm tested with a real address you own.
4. GitHub: webhook added, a test release sent to a test segment or test address.
5. Dashboard: `UPDATE_SUBSCRIBE_URL` set, box visible to admins.
6. Privacy policy published and linked from the form.
7. First real broadcast.

## 14. What I need from you

### Cloudflare

1. Is the domain on Cloudflare? Which zone, and which hostname should the Worker
   use (`updates.<domain>` or a `workers.dev` URL for now)?
2. Workers plan (Free/Paid) and whether `wrangler` is installed locally. Free is
   enough for this volume; WAF rate-limiting rules may need Pro.
3. How should I authenticate for research/deploy: a scoped API token exported in
   the shell, or `wrangler login`? Please do not paste a token in chat if it can
   be avoided. Scopes: Workers Scripts:Edit, Workers KV Storage:Edit, and
   Turnstile:Edit if you want me to create the widget.
4. Do you want Turnstile on the form? If yes, may I create the widget, or will
   you provide the site key/secret?
5. Is there an existing Worker/KV namespace to reuse, or should everything be new
   under `opamp-updates`?
6. Are Cloudflare rate-limiting rules available on your plan? If not, I will put
   the counters in a Durable Object instead of KV.
7. Should the Worker live in this repo (`cloudflare/updates-worker/`) or a
   separate repo?
8. May I create the `updates.<domain>` DNS record, or will you?
9. Is Cloudflare Access in front of anything that would block `/subscribe` or
   the GitHub webhook? Those two must be public.

### Resend

1. Plan (Free/Pro/Scale), whether Broadcasts and Segments are enabled, and the
   contact quota.
2. Which verified domain and `from` address should the mail use? Is the domain
   fully verified (SPF/DKIM/DMARC)? A subdomain is preferable.
3. Create (or let me create) the dedicated segment and give me its ID.
4. Create an API key with sending + contacts + broadcasts access. Put it in
   `wrangler secret put`, not in chat or the repo.
5. Open/click tracking: off (recommended) or on?
6. Default Resend unsubscribe page, or custom branding?
7. Do you want bounce/complaint webhooks into the Worker, or is the Resend
   dashboard enough for v1?
8. The footer text: project name, physical address (CAN-SPAM), privacy-policy
   URL, and a monitored reply-to address.
9. A test address you own for the first end-to-end send.

### GitHub

1. Do you want the **webhook** (recommended: the Resend key stays in Cloudflare)
   or a GitHub Actions workflow?
2. Are all releases created as **published, non-prerelease** GitHub Releases?
   `release.py` only pushes tags; confirm the release is created too.
3. Should prereleases email anyone? (Recommendation: no, or a separate segment.)
4. Email body: fixed template + link to the release (recommended), or include the
   release notes? If notes, plain text only.
5. Is `agardnerIT/opamp-server-py` the canonical repo, or are there mirrors that
   also publish releases?

### Product / legal

1. The privacy-policy URL and the data-controller name/contact for deletion.
2. The consent wording (I can draft it; you approve it).
3. Physical postal address for the footer, or a decision to use a registered
   agent/virtual address.
4. Box style: embedded iframe (recommended) or a "Subscribe" button/link.
5. Should the box be visible to every logged-in admin, or only on a settings
   page?
6. Do you want a "send test email" control in the dashboard? (Useful, but it
   needs an admin-only Worker endpoint.)

### Access rules

- I will not ask for your Resend or Cloudflare password.
- Use a scoped API token / API key, exported in the shell or stored in a
  `0600` file; rotate it after the work if it ever appears in a log.
- I will do read-only research first (zone, plan, existing Workers, domain
  verification, segments) and list the exact resources I intend to create
  before creating them.
- I will deploy only after you confirm the resource list and the test address.

## 15. Open decisions

1. Double opt-in: **yes** (recommended; without it the endpoint is an
   email-bombing tool and the list is unconsented).
2. Turnstile: **yes** (recommended with double opt-in, not instead of it).
3. Worker hostname: `updates.<domain>` vs `workers.dev`.
4. Iframe vs server-side POST.
5. Release notes in the email: link only (recommended) vs plain-text excerpt.
6. Prereleases: skip (recommended) vs separate segment.
7. Tracking: off (recommended) vs on.
8. Retention for unsubscribed contacts: delete after 30/90 days vs keep for
   suppression.
