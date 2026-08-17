# PeerBridge Edge Intake

This optional Cloudflare Worker is the public HTTPS validation boundary for PeerBridge
feedback and the independent announcement feed. It is not required for local coordination.
The maintained Alpha deployment uses D1, a private R2 bucket, and Google Apps Script.
R2 stores the validated private ZIP for a bounded retention period; Gmail provides the
maintainer notification and a second private delivery path.

## Security boundary

- The Worker validates the ZIP, stores it in a private R2 bucket, and commits only bounded
  case metadata plus the exact object key and SHA-256 to D1. D1 never stores bundle bytes.
- The HMAC-protected Google Apps Script receiver sends the maintainer notification. A
  notification failure does not discard a case already committed to R2 and D1.
- No endpoint lists or downloads feedback without `ADMIN_TOKEN`.
- Raw client IP addresses are never stored. A server-secret HMAC is used only for daily
  abuse limits.
- Provider API keys may exist only inside a locally encrypted bundle. The Worker never
  decrypts them.
- Gmail notification is performed by a private Google Apps Script backend. The public desktop
  never receives its URL or the shared HMAC secret, and unsigned direct requests cannot mail.
- Gmail receives the validated private ZIP as an attachment plus server-bound case metadata
  and a strictly validated reply address. Caller-controlled summaries and version text are
  not copied into the email body. Any optional full credential is already encrypted locally
  to the pinned maintainer public key inside the ZIP.
- Announcement publishing requires `ADMIN_TOKEN`. Announcement text cannot execute code,
  invoke an Agent, or contain non-HTTPS links.
- Do not commit `wrangler.jsonc`, `.dev.vars`, binding IDs, admin tokens, rate salts, digest
  secrets, or a feedback decryption private key.

## Setup

1. Run `npm install` in this directory.
2. Create a D1 database and a private R2 bucket in the maintainer Cloudflare account.
3. Copy `wrangler.jsonc.example` to ignored `wrangler.jsonc` and replace only binding IDs.
4. For a new database, apply `schema.sql` to local and remote D1. For an existing
   Alpha database created before the per-submission commit marker was added, apply
   `migrations/0002_feedback_submission_id.sql` once before deploying this Worker;
   then apply `migrations/0003_tighten_feedback_rate_limits.sql` and
   `migrations/0004_feedback_retention_and_tiered_limits.sql`, followed by
   `migrations/0005_global_feedback_attempt_cap.sql`, in that order. Apply
   `migrations/0006_feedback_object_cleanup.sql`,
   `migrations/0007_feedback_notification_retry.sql`, and
   `migrations/0008_feedback_notification_claim.sql` in that order to both new and
   existing databases.
   Do not re-run `schema.sql` over a live database as an upgrade mechanism.
5. Add an independent R2 lifecycle rule for the `feedback/` prefix. The Worker cron is the
   primary metadata-aware deletion path; this bucket rule is the object-retention backstop:

   ```powershell
   npx wrangler r2 bucket lifecycle add peerbridge-feedback-bundles `
     peerbridge-feedback-30-day feedback/ --expire-days 30
   ```

6. Add independent cryptographically random secrets with
   `wrangler secret put ADMIN_TOKEN` and `wrangler secret put RATE_SALT`.
   Each must be 32-256 non-whitespace characters; weak or incomplete
   configuration makes the Worker fail closed.
7. Deploy the private Google Apps Script backend first. Store its exact `/exec` URL with
   `wrangler secret put GOOGLE_APPS_SCRIPT_URL`, and store the same 43-256 character opaque
   secret used by the Script Property with
   `wrangler secret put GOOGLE_APPS_SCRIPT_SECRET`. Never put either value in source,
   Wrangler vars, desktop configuration, logs, or screenshots.
8. Optionally configure `DIGEST_WEBHOOK_URL` and `DIGEST_SHARED_SECRET` for a bounded
   secondary digest. R2 remains the authoritative temporary bundle store.
9. Run tests, deploy, and submit a synthetic non-secret feedback case before binding the
   public application configuration.

## Deployment verification

The public health response exposes only binding presence and the required retention contract;
it does not expose tokens, binding IDs, bucket names, or lifecycle credentials. Check it after
every deployment without an admin or Worker secret:

```powershell
$health = Invoke-RestMethod https://YOUR_WORKER/health
if (-not $health.bindings.bundles -or
    -not $health.retention.r2_lifecycle_rule.required -or
    $health.retention.r2_lifecycle_rule.prefix -ne 'feedback/' -or
    $health.retention.r2_lifecycle_rule.expiration_days -ne 30) {
  throw 'Worker R2 retention contract is not release-ready.'
}
```

Separately verify the actual bucket configuration through the maintainer's existing Wrangler
login profile; no Worker application secret is read or supplied:

```powershell
npx wrangler r2 bucket lifecycle list peerbridge-feedback-bundles
```

The output must contain an enabled `peerbridge-feedback-30-day` rule for prefix `feedback/`
with expiration after 30 days. A passing health response alone does not prove that the
Cloudflare bucket rule exists.

The public desktop configuration uses JSON transport:

```json
{
  "endpoint": "https://YOUR_WORKER/v1/feedback",
  "endpoint_transport": "json-base64-v1"
}
```

Routine detailed review should use the authenticated Cloudflare admin endpoints with a
locally protected admin token. The desktop does not ship or persist that token. Cloudflare
dashboard login is needed only for initial setup and infrastructure troubleshooting.

Accepted D1 case metadata and its matching R2 object target 30-day retention. The Gmail
copy follows the maintainer mailbox retention policy. The hourly scheduled task deletes
the R2 object before expired D1 metadata in bounded batches, and the independent R2 lifecycle
rule bounds object retention if that task cannot run. If the R2 binding is missing or deletion
fails, D1 metadata is retained so an object cannot become untracked. Failed intake rollback
deletes create durable D1 cleanup records for the scheduled retry path. Anonymous malformed
attempts have an independent per-source limit, so invalid traffic cannot consume the
accepted global case allowance; valid new cases are capped independently per source and
globally. Duplicate submissions remain idempotent and do not consume a second accepted-case
slot.

Cloudflare Email Service remains an optional domain-backed fallback through the
`FEEDBACK_EMAIL` and `FEEDBACK_EMAIL_FROM` bindings. It is not needed for the zero-domain
Apps Script route and is intentionally absent from the committed Wrangler example.

The R2 route accepts validated ZIP bundles within the Worker's bounded upload limit. If R2
is unavailable, the Worker may use the 8 MiB email-only fallback; larger fallback bundles
fail closed and remain in the user's local feedback folder. The authenticated admin bundle
endpoint never exposes data without `ADMIN_TOKEN`.
