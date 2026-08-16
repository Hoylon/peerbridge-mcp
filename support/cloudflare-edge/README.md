# PeerBridge Edge Intake

This optional Cloudflare Worker is the high-capacity private destination for PeerBridge
feedback and the independent announcement feed. It is not required for local coordination.

## Security boundary

- Feedback ZIP bodies are private R2 objects. D1 stores only bounded case metadata.
- No endpoint lists or downloads feedback without `ADMIN_TOKEN`.
- Raw client IP addresses are never stored. A server-secret HMAC is used only for daily
  abuse limits.
- Provider API keys may exist only inside a locally encrypted bundle. The Worker never
  decrypts them.
- Announcement publishing requires `ADMIN_TOKEN`. Announcement text cannot execute code,
  invoke an Agent, or contain non-HTTPS links.
- Do not commit `wrangler.jsonc`, `.dev.vars`, binding IDs, admin tokens, rate salts, digest
  secrets, or a feedback decryption private key.

## Setup

1. Run `npm install` in this directory.
2. Create a D1 database and an R2 bucket in the maintainer Cloudflare account.
3. Copy `wrangler.jsonc.example` to ignored `wrangler.jsonc` and replace only binding IDs.
4. For a new database, apply `schema.sql` to local and remote D1. For an existing
   Alpha database created before the per-submission commit marker was added, apply
   `migrations/0002_feedback_submission_id.sql` once before deploying this Worker;
   do not re-run `schema.sql` over a live database as an upgrade mechanism.
5. Add secrets with `wrangler secret put ADMIN_TOKEN` and
   `wrangler secret put RATE_SALT`.
6. Optionally configure `DIGEST_WEBHOOK_URL` and `DIGEST_SHARED_SECRET` for one bounded
   Gmail digest per schedule. Gmail is notification-only; it is not the source of truth.
7. Run tests, deploy, and submit a synthetic non-secret feedback case before binding the
   public application configuration.

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
