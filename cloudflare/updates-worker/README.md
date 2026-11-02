# `opamp-updates` — release email Worker

A one-way Cloudflare Worker between the self-hosted dashboard and the project's
Resend account. It exposes only these endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | signup form (email + consent + Turnstile) |
| `POST` | `/subscribe` | double opt-in: stores a 24 h token, sends a confirmation email |
| `GET` | `/confirm?token=` | adds the confirmed address to the Resend segment |
| `GET` | `/privacy` | release-list privacy policy |
| `GET` | `/delete` | deletion request form |
| `POST` | `/delete` | double opt-in deletion: sends a confirmation link |
| `GET` | `/delete/confirm?token=` | removes the address from the release segment |
| `POST` | `/release` | GitHub `release: published` webhook → Resend Broadcast |
| `GET` | `/health` | configuration booleans; no secrets |

There is **no** endpoint to list or read contacts. Deletion is a two-step,
confirmed flow, not a one-shot public delete. The Worker holds a
Resend `full_access` key (Resend only offers `full_access` or `sending_access`,
and contacts/broadcasts need the former), so it is the trust boundary: keep the
code small, review changes, and rotate the key if it is ever exposed.

## Deployed URL

```
https://opamp-updates.tame-lamp8409.workers.dev
```

## Local development

```bash
cd cloudflare/updates-worker
npm install
cp .dev.vars.example .dev.vars   # fill in what you are testing
npx wrangler dev                 # http://localhost:8787
npm test                         # 38 Worker tests, no network
npm run typecheck
```

With `RESEND_API_KEY` unset, `/subscribe` returns 503 and `/health` reports
`configured.resend: false`. Turnstile is skipped when `TURNSTILE_SECRET` is
unset, which is the local-only path.

## Configuration

Edit the `vars` block in `wrangler.jsonc` and redeploy:

| Var | Meaning |
|---|---|
| `PUBLIC_BASE_URL` | used in confirmation links |
| `RESEND_SEGMENT_ID` | the **only** segment this Worker may write to |
| `RESEND_FROM` | verified sender, e.g. `opamp-server-py <updates@example.com>` |
| `RESEND_REPLY_TO` | optional monitored reply address |
| `TURNSTILE_SITE_KEY` | public widget key; leave empty to disable |
| `PRIVACY_URL` | override the policy link; default is `${PUBLIC_BASE_URL}/privacy` |
| `PRIVACY_CONTACT` | deletion/access contact shown in the policy |
| `DASHBOARD_URL` | "back to the dashboard" link on the confirmation page |

Secrets (never in `wrangler.jsonc`, never in git):

```bash
npx wrangler secret put RESEND_API_KEY
npx wrangler secret put GITHUB_WEBHOOK_SECRET
npx wrangler secret put TURNSTILE_SECRET
npx wrangler deploy
```

`RESEND_SEGMENT_ID` is not a secret, but it is deliberately a **single** value:
the Worker cannot broadcast to another segment.

## Resend setup

1. Verify the sending domain (SPF, DKIM, DMARC). A subdomain such as
   `updates.example.com` is preferable.
2. Create a segment, e.g. `opamp-server-py release announcements`; put its ID in
   `RESEND_SEGMENT_ID`.
3. Create a `full_access` API key; store it with `wrangler secret put`.
4. Turn **open and click tracking off**. This is an announcement list, not an
   analytics product.
5. Keep the default Resend unsubscribe page, or brand it. Every broadcast
   includes `{{{RESEND_UNSUBSCRIBE_URL}}}`; Resend flips the contact's global
   subscription status when it is clicked.

## GitHub webhook

Repo → Settings → Webhooks → Add webhook:

- Payload URL: `https://opamp-updates.tame-lamp8409.workers.dev/release`
- Content type: `application/json`
- Secret: the same value as `GITHUB_WEBHOOK_SECRET`
- Events: **Releases** only

The Worker verifies `X-Hub-Signature-256`, handles `published` and `released`
(release-from-draft), ignores drafts and prereleases, and
deduplicates by `release.id` in KV for 30 days. GitHub retries failed deliveries;
a retry cannot double-send. Resend's own `Idempotency-Key` covers `/emails` but
not `/broadcasts`, which is why the KV guard exists.

The release must be **published** and not a prerelease. `release.py` in the repo
pushes tags; make sure a GitHub Release is created for each tag, or the webhook
never fires.

## What is stored

| Where | What | TTL |
|---|---|---|
| KV `pending:<token>` | email + timestamp, unconfirmed | 24 h |
| KV `sent:<sha256(email)>` | hash only, anti-repeat | 10 min |
| KV `release:<release.id>` | id only, anti-double-send | 30 days |
| KV `delete:<token>` | email + timestamp, unconfirmed deletion | 24 h |
| KV `delete-sent:<sha256(email)>` | hash only, anti-repeat | 10 min |
| Resend contact | email, subscribed status, timestamps | until deleted |

No names, hostnames, instance IDs, versions, fleet data, or IP addresses are
stored by the Worker. Cloudflare request logs still contain IPs; set the Worker
log retention deliberately.

## Privacy policy and deletion

`GET /privacy` serves the policy in `src/privacy.ts`. It is a draft: review the
controller name, contact and retention wording before pointing the dashboard at
this Worker.

Deletion is also double opt-in. `POST /delete` sends a confirmation link only if
the address is in the release segment; the response is identical whether or not
it is, so the endpoint cannot be used to enumerate subscribers. The link removes
the address from **this** segment. The global Resend contact is deleted only when
no other segment still uses it, so a Resend account shared with another project
is not damaged.

A data processing agreement is a legal document between the controller and
Resend/Cloudflare, not an app page. The policy links both processors' privacy
policies; use their standard DPAs if one is required.

## Tests

`npm test` runs 38 offline tests with a fake KV and a mocked `fetch`:

- generic response and one email on subscribe; no second email inside dedupe;
- consent, email validation, body size, Turnstile failure;
- confirm creates a segmented contact, replay/expiry is rejected, a Resend
  failure leaves the link usable;
- release webhook: valid HMAC, invalid HMAC, draft/prerelease skip, idempotent
  replay, non-semver tag;
- privacy page and deletion form render; deletion emails only a segment member;
  a non-member and a missing contact get the same response and no email;
  confirmation removes the segment membership, deletes the orphan contact,
  keeps a contact used by another segment, rejects replays and keeps the token
  when Resend fails;
- the broadcast contains the unsubscribe merge tag;
- no email address reaches the logs;
- `/health` never exposes secrets.
