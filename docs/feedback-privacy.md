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

The local Alpha opens a private email draft addressed to the configured support mailbox and
keeps the sealed ZIP on the user's computer for explicit attachment. Automatic HTTPS intake
is not claimed until a later release publishes and verifies a maintainer-controlled endpoint.
The normal report and user-selected safe attachments are **not** end-to-end encrypted by
PeerBridge; they receive only the protection provided by the user's mail transport and Gmail.
Only the explicitly enabled complete-credential envelope is encrypted end to end to the
pinned maintainer public key.

GitHub hosts this public notice and the release metadata. Gmail is the support-mail processor
for this Alpha. PeerBridge sends no feedback to a model provider and has no hidden feedback
collector. The maintainer uses a submitted case only to diagnose PeerBridge and provider
onboarding compatibility problems. A reply is possible only when the user supplies a valid
contact address.

The maintainer targets deletion of resolved feedback mail, bundles, and attachments within
30 days after resolution. Gmail trash/backup behavior remains subject to Google's service
terms and cannot be shortened by PeerBridge. The encrypted credential, when supplied, is not
retained separately from its case bundle. Users retain control of local outbox copies and
must delete those copies themselves.

Users should remove unrelated personal information before submission. A user may request
deletion by emailing the configured support address with the case ID shown by PeerBridge.
The maintainer will acknowledge the request and delete the active support copy within 30 days;
the response cannot promise erasure from a mail processor's already-created backups.
