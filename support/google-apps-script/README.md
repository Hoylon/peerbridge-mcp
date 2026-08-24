# Google Apps Script private feedback intake

This is PeerBridge's zero-domain private mail backend. The public Cloudflare Worker forwards
an HMAC-authenticated `peerbridge.feedback-upload.v1` JSON envelope, and this backend sends
the verified private feedback ZIP to the mailbox stored in the private Apps Script property
`PEERBRIDGE_SUPPORT_EMAIL`. D1 keeps bounded case metadata while the private R2 bucket retains
the matching validated ZIP for the configured expiry period.

## Security boundary

- The destination is read only from `PEERBRIDGE_SUPPORT_EMAIL`; request data cannot select a
  recipient and the address is not committed to the public repository.
- The receiver accepts only an HMAC-bound fixed envelope, a strictly validated reply address,
  and one canonical base64 ZIP. It re-decodes the ZIP, enforces the 8 MiB application limit,
  checks the ZIP signature, and recomputes SHA-256 before sending.
- Duplicate case IDs are idempotent; the same ID with different bytes is rejected.
- At most 20 new cases are accepted per UTC day. Anonymous feedback stops before the final
  20 recipients of remaining Google mail quota, reserving that capacity for protected
  operational notifications.
- Mail handoff is fail-closed. A `sendEmail` exception or interrupted post-send receipt write
  leaves an operator-visible uncertain record and never causes an automatic resend. At most
  20 unresolved deliveries can exist, and each case is limited to three delivery attempts.
- The email body contains only the reply address and server-bound case metadata. The ZIP is
  attached. If the user explicitly included a full provider credential, it was encrypted
  locally with the packaged support public key before upload; Apps Script cannot decrypt it.
- Unsigned or stale direct requests are rejected before mail delivery.
- The endpoint is private delivery, not an authentication service. It must never receive a
  maintainer private key. Its URL and HMAC secret must never ship in the desktop package.

## Deploy

1. Sign in to the Google account that will own the private receiver.
2. Create an Apps Script Web app and replace its files with `Code.gs` and
   `appsscript.json` from this directory. With clasp, run `clasp login`, `clasp create
   --type webapp --title "PeerBridge Feedback"`, and `clasp push` from a temporary copy
   containing these two files.
3. In Apps Script Project Settings, add Script Property
   `PEERBRIDGE_INGRESS_HMAC_SECRET` with a randomly generated 43-256 character opaque value,
   plus `PEERBRIDGE_SUPPORT_EMAIL` with the maintainer mailbox. Do not paste either value into
   source or screenshots. The helper functions `configureIngressSecret(...)` and
   `configureSupportEmail(...)` provide the same setup path from the editor.
4. Deploy as a Web app. The committed manifest binds execution to the deploying maintainer
   and access to `ANYONE_ANONYMOUS`, so Alpha users can submit without a Google account.
   HMAC authentication in `Code.gs` is the actual mail gate. Confirm the mail permission
   prompt and verify the deployment settings.
5. Copy the deployed URL ending in `/exec`. Never use the `/dev` test URL.
6. Store that URL and the same HMAC value only as Cloudflare Worker secrets named
   `GOOGLE_APPS_SCRIPT_URL` and `GOOGLE_APPS_SCRIPT_SECRET`. The packaged desktop endpoint
   must remain the Cloudflare `/v1/feedback` URL using `json-base64-v1`.
7. Submit one synthetic non-secret case from the packaged desktop build. Publication remains
   blocked until the local case ID and SHA-256 match the HTTPS receipt and received email.

## Delivery reconciliation

Run `listDeliveryReconciliation` from the Apps Script editor after a `delivery_failed`,
`delivery_state_uncertain`, or `delivery_reconciliation_required` result. It returns only
case IDs, bundle SHA-256 values, state, bounded attempt counts, timestamps, and fixed error
codes as JSON in the execution log. It does not return reply addresses, bundle bytes, or
decrypted content. Unresolved records do not expire automatically; sent records expire 31
days after their last resolution.

For every listed case, search the maintainer mailbox, including spam, for the exact subject
`PeerBridge feedback <case_id>`, then verify the SHA-256 in the message body:

To call the parameterized action from the Apps Script editor, add a temporary no-argument
wrapper that returns `reconcileDelivery("<case_id>", "<bundle_sha256>", "<action>")`, run
the wrapper, verify the privacy-safe JSON result in the execution log, and remove the wrapper
before deploying.

1. If the matching message exists, run
   `reconcileDelivery("<case_id>", "<bundle_sha256>", "mark_delivered")`. This closes the
   uncertain state without sending mail.
2. If mailbox inspection confirms that no matching message exists, run
   `reconcileDelivery("<case_id>", "<bundle_sha256>", "allow_retry")`. This only arms one
   retry; it does not send anything. The next fresh HMAC-authenticated envelope for that exact
   case and SHA-256 consumes the authorization and makes one delivery attempt.
3. For email-only intake, retry the original desktop submission after arming the case. For an
   R2-retained case whose public upload already succeeded, use the authenticated admin bundle
   to complete delivery manually, verify the received message, and use `mark_delivered`;
   duplicate public uploads do not retrigger notification.

Never use `allow_retry` until the mailbox check is complete. If all three attempts are
uncertain or failed, the case remains blocked against duplicate delivery. After confirming
that no delivery occurred, submit a new case rather than deleting or editing Script
Properties. The daily counter is charged once for the original case, not again for its
operator-authorized retries, while Google's remaining mail quota is checked before every
attempt. Retries do not consume the protected quota reserve either.

Apps Script is appropriate for low-volume Alpha mail delivery and avoids buying a sending
domain. Cloudflare remains the public validation, rate-limit, private R2 retention,
announcement, D1 metadata, and authenticated admin boundary.
