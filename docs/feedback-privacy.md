# PeerBridge Feedback Privacy

PeerBridge feedback is provider-independent. It is never posted into an Agent room or sent
to a configured model provider.

## Normal report

A normal report can include the user's message, optional reply address, explicitly selected
safe attachments, application version, locale, operating-system family, Python version, and
bounded parser/error diagnostics. Likely credentials and personal home-directory segments
are redacted before the report is sealed.

## Optional complete credential

The complete-credential option is off by default. When a user explicitly enables it, the
credential is encrypted on the user's computer to the release-bound PeerBridge support
public key. Plaintext is not written to the feedback ZIP, project database, MCP messages,
logs, analytics, or Git. The encrypted member can be opened only with the maintainer's
separate Windows DPAPI-protected private identity.

## Delivery and retention

The packaged Alpha submits the sealed ZIP to a maintainer-controlled HTTPS endpoint. The
endpoint validates the archive, stores the private bundle temporarily in a non-public R2
bucket, records bounded case metadata in D1, and sends a private maintainer notification.
If HTTPS delivery is unavailable, PeerBridge keeps the sealed bundle locally so the user can
retry without re-entering a credential. The normal report and user-selected safe attachments
are **not** end-to-end encrypted by PeerBridge; they rely on HTTPS and the configured private
storage/mail processors. Only the explicitly enabled complete-credential envelope is encrypted
end to end to the pinned maintainer public key before it leaves the user's computer.

GitHub hosts this public notice and the release metadata. Cloudflare processes the HTTPS
request and temporary private bundle; Gmail is the maintainer notification/mail processor.
PeerBridge sends no feedback to a model provider and has no hidden feedback collector. The
maintainer uses a submitted case only to diagnose PeerBridge and provider onboarding
compatibility problems. A reply is possible only when the user supplies a valid contact
address.

The HTTPS intake targets deletion of private R2 bundles and D1 case metadata after 30 days.
An independent 30-day R2 lifecycle rule bounds object retention if the Worker's scheduled
metadata-aware cleanup cannot run. The Worker deletes an R2 object before deleting its D1
metadata; if the R2 binding or delete operation is unavailable, it keeps the metadata for a
later retry rather than leaving an untracked object. Failed intake rollback deletes also leave
a durable D1 cleanup record. The maintainer targets deletion of resolved feedback mail and
attachments within 30 days after resolution. Cloudflare and Gmail backup behavior remains
subject to their service terms and cannot be shortened by PeerBridge. The encrypted credential,
when supplied, is not retained separately from its case bundle. Users retain control of local
outbox copies and must delete those copies themselves.

Users should remove unrelated personal information before submission. A user may request
deletion by emailing the configured support address with the case ID shown by PeerBridge.
The maintainer will acknowledge the request and delete the active support copy within 30 days;
the response cannot promise erasure from a mail processor's already-created backups.
