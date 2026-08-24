# Provider routing contract

## Status

This document defines pre-release security requirements. It is not evidence that
the requirements are implemented, tested, enabled, or released. A route may be
advertised as available only after its runtime receipts and adversarial tests
demonstrate every applicable requirement below.

## Identity and evidence classes

`official`, `relay`, and `local` are evidence classes, not assertions about the
ultimate upstream model operator:

- `official` means that the run presents verifiable evidence for an approved
  provider-controlled client or endpoint. A model name or compatible API shape
  is not sufficient.
- `relay` means that an intermediary handled the request. Any upstream provider
  or model name returned by that intermediary remains a claim unless separately
  attested.
- `local` means that the observed runtime and endpoint are local. It does not
  prove who built the weights or where they originated.

Receipts and user interfaces MUST keep the requested evidence class, observed
transport, configured provider, claimed model, and independently verified
provider evidence in separate fields. They MUST NOT upgrade a route based on a
provider name, model string, API compatibility, or operator expectation.

## Credential descriptors and targets

A credential descriptor MUST bind all of the following before a secret can be
loaded:

- descriptor schema version;
- stable route ID and route evidence class;
- stable provider ID and adapter/backend type;
- endpoint-record ID or normalized endpoint identity;
- credential slot and credential target; and
- an opaque credential version.

Every lookup MUST compare the complete descriptor with the requested route and
provider. A missing field, mismatch, ambiguous match, or cross-class reuse MUST
fail closed. The coordination database and receipts must never contain the raw
secret or a secret-derived identifier.

Version 2 credential targets MUST be collision-free within the local registry.
They MUST use stable, uniqueness-constrained route/provider identifiers and an
explicit slot, rather than lossy sanitized display names or truncated hashes.
New writes use only v2 targets.

Legacy targets may be read solely for an explicit migration. Migration MUST:

1. resolve exactly one legacy record and verify its route/provider binding;
2. create the v2 target without overwriting another target;
3. read back and validate the v2 record before selecting it;
4. record a content-free migration receipt; and
5. leave legacy deletion to a separate, explicitly authorized operation.

The migration is idempotent and fail-closed. An ambiguous or partially migrated
legacy record is not eligible for inference.

The credential version MUST be a cryptographically random opaque value generated
independently of the secret. It MUST NOT be a hash, prefix, suffix, checksum, or
fingerprint of the API key. Rotation creates a new random version. Logs, events,
receipts, database rows, and MCP messages may bind the opaque version, but never
a value derived from credential bytes.

## Endpoint and process boundaries

Remote endpoints require encrypted transport. URL user-info, fragments, and
credential-bearing query strings are forbidden. Provider discovery and inference
clients MUST disable redirects. Every `3xx` response fails closed; authorization
headers and API keys are never forwarded to another origin.

Provider clients and adapter subprocesses MUST start in a controlled, canonical
runtime working directory selected by policy. They MUST NOT inherit an arbitrary
caller working directory, use a credential-storage directory as `cwd`, or resolve
executables/configuration through untrusted relative paths. Runtime receipts bind
only a non-secret directory identity and the selected executable/configuration
hashes.

Direct OpenAI-compatible connections are first-class routes and do not depend on CC
Switch. Their secrets remain in the operating-system credential store. The
coordination database may retain only non-secret connection and route bindings,
endpoint identity hashes, and opaque credential versions.

The requested model and the model reported by a completion are separate identities.
Each route binds `model_id` and may bind `response_model_id`; if the latter is absent,
it is exactly equal to `model_id`. A relay deployment alias is acceptable only when
configured explicitly and established by real discovery and inference evidence. The
runner MUST reject any other response model and record requested, expected-response,
and observed-response identities separately in its content-free receipt.

Each route may also bind an explicit `inference_timeout_seconds` from 1 to 300.
Omitting it keeps the audited defaults: 60 seconds for relay/local HTTP requests and
180 seconds for native ACP. Slow build/reasoning routes must use a new immutable route
ID with an explicit override; timeout policy must never be inferred from a model name,
response alias, provider label, or credential backend.

Model discovery receipts bind a canonical registry containing only sorted,
deduplicated model IDs. Provider ordering, duplicate entries, and unrelated response
metadata do not alter the registry hash. A model list never proves upstream provider
identity or upgrades a `relay` route to `official`.

Optional CC Switch discovery MUST use its public CLI and return only redacted provider
identity and advertised model IDs. It must not read the CC Switch database. Before
inference, the adapter MUST verify that the selected route advertises the requested
model. A response model that differs from both the request and the explicit
`response_model_id` fails the model-identity gate; it never upgrades the route evidence
class.

## Operations, retries, and idempotency

Each logical provider request and MCP operation MUST have one operation ID and,
where supported, one idempotency key that remains stable across safe retries.
Implementations MUST classify an operation before dispatch as:

- read-only and replay-safe;
- idempotent mutation with a verified idempotency contract; or
- non-idempotent or unknown.

Retry state MUST distinguish pre-dispatch failure, acknowledged response,
ambiguous completion, completed operation, and terminal rejection. Only bounded
transient failures may retry. Authentication, authorization, policy, schema, and
other deterministic client failures are terminal. An ambiguous mutation or an
operation with unknown idempotency MUST NOT be replayed automatically. Retries
use bounded backoff, retain the original operation identity, and consume the same
operation budget. Cancellation remains cooperative at provider request,
retry-backoff, and MCP tool-call boundaries.

## MCP call and result budgets

Every model operation MUST receive finite cumulative budgets, not only per-call
limits. At minimum the policy MUST cap:

- total MCP tool calls, including nested calls and retries;
- cumulative tool-argument and tool-result bytes or tokens;
- individual result size, nesting depth, elapsed time, and retry count; and
- cumulative model/tool round trips for the operation.

All attempted calls consume budget, including failed and retried calls. Missing
limits are a configuration error. Budget exhaustion produces a typed fail-closed
result and an audit event; it must not silently parse truncated structured data,
continue with partial evidence, or start a second operation to evade the limit.

Only explicitly allowlisted read-only MCP tools may be exposed by default. Any
mutation requires a separately authorized policy and idempotency classification.
Tool arguments, results, prompts, and model output remain outside content-free
receipts; receipts bind their hashes, sizes, operation order, budget accounting,
and terminal status.

## Release gate

Configuration examples, unit tests, or this document do not satisfy the release
gate. Release evidence requires real-client receipts covering descriptor mismatch,
v2 migration, random-version rotation, redirect rejection, controlled `cwd`,
retry ambiguity, idempotent duplicate suppression, cumulative budget exhaustion,
and accurate evidence-class reporting without exposing content or credentials.
