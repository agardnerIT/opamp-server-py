import type { Env } from "./env";
import { escapeHtml } from "./validate";

export const PRIVACY_LAST_UPDATED = "2 November 2026";

/**
 * The public privacy policy for the release-announcement list. It describes
 * only this list: the self-hosted dashboard is operated by whoever deploys it
 * and has its own data.
 *
 * This is a draft for the project owner to review. The controller name and
 * contact come from env so they can be corrected without touching the markup.
 */
export function privacyPolicyHtml(env: Env, deleteUrl: string): string {
  const contact = env.PRIVACY_CONTACT || "privacy@agardner.net";
  const contactLink = `<a href="mailto:${escapeHtml(contact)}">${escapeHtml(contact)}</a>`;
  const deleteLink = deleteUrl
    ? `<a href="${escapeHtml(deleteUrl)}">delete your address</a>`
    : "use the deletion link in any email";

  return `
<h1>Privacy policy — opamp-server-py release emails</h1>
<p class="note">Last updated: ${escapeHtml(PRIVACY_LAST_UPDATED)}</p>

<p>This policy covers the release-announcement email list for the
<strong>opamp-server-py</strong> project. It does not cover a self-hosted
opamp-server-py dashboard, which is operated by whoever deployed it.</p>

<h2>Who is responsible</h2>
<p>Adam Gardner, the opamp-server-py project ("we"). Contact:
${contactLink}.</p>

<h2>What we collect</h2>
<ul>
  <li>the email address you submit;</li>
  <li>the time you confirmed the subscription;</li>
  <li>your subscription status and the release emails we send you.</li>
</ul>
<p>We do not collect names, hostnames, instance IDs, software versions, fleet
data, or IP addresses in our own storage. The form is served by Cloudflare,
which processes request metadata (including your IP address) to deliver and
protect the service; see Cloudflare's privacy policy below.</p>

<h2>Why we use it</h2>
<p>To send one email when a new opamp-server-py release is published. Nothing
else. We do not sell, rent, or share the list.</p>

<h2>Lawful basis</h2>
<p>Consent. You give it by submitting the form and clicking the confirmation
link in the email we send you. You can withdraw it at any time by
unsubscribing or deleting your address.</p>

<h2>Who processes it</h2>
<ul>
  <li><strong>Resend</strong> delivers the email and stores the contact record.
      <a href="https://resend.com/legal/privacy-policy">Resend privacy policy</a>.</li>
  <li><strong>Cloudflare</strong> hosts the signup Worker and processes request
      metadata. <a href="https://www.cloudflare.com/privacypolicy/">Cloudflare
      privacy policy</a>.</li>
</ul>
<p>They act as processors on our behalf. Both may process data outside your
country; their privacy policies describe the safeguards they use.</p>

<h2>How long we keep it</h2>
<p>We keep your address until you unsubscribe or ask us to delete it.
Unsubscribing stops all future release emails but keeps a suppression record so
you are not emailed again. Deleting removes you from the release list and, if
the address is not used for anything else in our email provider, deletes the
contact record too.</p>

<h2>Your choices</h2>
<ul>
  <li><strong>Unsubscribe:</strong> every release email has an unsubscribe link.</li>
  <li><strong>Delete:</strong> ${deleteLink}.</li>
  <li><strong>Access, correct, or restrict:</strong> email ${contactLink}.</li>
  <li><strong>Complain:</strong> contact your local data-protection authority.</li>
</ul>

<h2>Tracking</h2>
<p>No open pixels, no click tracking, no advertising trackers, no profiling.</p>

<h2>Changes</h2>
<p>If this policy changes, the date at the top changes. Material changes will be
announced to the list before they take effect.</p>

<h2>Contact</h2>
<p>${contactLink}</p>
`;
}
