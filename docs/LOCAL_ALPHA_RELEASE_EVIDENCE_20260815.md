# PeerBridge Local Alpha Release Evidence

Date: 2026-08-16 (Asia/Taipei)

This report records the current **local Alpha candidate** gates. It does not authorize a
GitHub publication, claim Stable status, or include the remote/mobile product profile.

## Regression and focused gates

- Full suite: 517 collected, 516 passed, 1 intentionally skipped. The first attempt was
  invalidated by a Windows permission error in the legacy global pytest temporary root;
  the authoritative run used a new project-local isolated `--basetemp` and completed
  without failures.
- Optional encrypted-feedback coverage is included in that full run: all 25 Feedback tests
  passed with locally installed, SHA-256-verified binary dependencies.
- Focused localization, attachment, and drag-interaction suite: 13 passed, 1 intentionally
  skipped.
- Package UI self-test: every navigation page plus composer, credential plane, room
  automation, localization, update check, and attachment controls passed.
- Visible desktop review: the normal chat surface fits at 1320 x 820 and the Traditional
  Chinese, Simplified Chinese, and English control labels render without observed clipping.
- Refresh continuity acceptance: unchanged chat, Agent cards, room seats, and Agent canvas
  projections no longer destroy and recreate widgets. New chat rows append incrementally and
  retain the reader's scroll position. The operator physically confirmed that refresh no
  longer flashes on 2026-08-15; the frozen candidate now adds a small localized successful
  refresh timestamp and a permanent English `Language` label beside the locale selector.

## Memory and lifecycle evidence

- Receipt: `.peerbridge/receipts/local-alpha-soak-20260816T061512Z.json`
- File SHA-256: `14f620bde6320806974b2d7a685e8b71ed0a34ee59b4216d7647733231a1dde0`
- Embedded receipt SHA-256: `26571e17d4ac29cbd4b0b33006b9dd9a64fe6c4af9b9ec84cdc3f30a78c4bfbe`
- Messages: 1,200
- Private-memory plateau growth: 4,096 bytes across the receipt's final four samples,
  below the 8 MiB acceptance limit.
- Audit chain, 1,202 audit events, database restart, crash slot release, singleton lock release, and duplicate
  runtime/supervisor rejection all passed.
- Live recovery observation at 2026-08-15 11:31 Asia/Taipei: the control-room monitor was
  still responsive while the `peerbridge-main` mailbox-supervisor process lock was no longer
  held. Exactly one replacement supervisor was started without relaunching the monitor; the
  same advisory lock then reported `HELD`. The resulting runtime had one visible monitor
  window (PID 1948, about 47.5 MB working set at observation) and one newly started hidden
  supervisor child (PID 15156, about 22.8 MB). No duplicate monitor or second lock owner was
  created.
- The running Codex desktop application's memory is outside PeerBridge's process boundary and
  is not represented as a PeerBridge pass or failure.

## Strict package evidence

The exact command profile and final candidate directory are recorded in the create-only
release receipt named below; the source document intentionally does not hard-code a mutable
candidate sequence number.

- Latest pre-freeze development result: `DEV_CHECK_OK files=144 version=0.1.0a2
  release_ready=false strict_release_required=true`.
  The final frozen-source result is authoritative only when it is reproduced in the
  create-only receipt described below.
- Remote/mobile evidence: explicitly outside this release profile.
- The exact wheel/source-distribution byte counts, SHA-256 values, command profile, test
  counts, and release-source-tree SHA-256 are bound in the ignored create-only receipt
  `.peerbridge/receipts/local-alpha-release-final-v3.json`. Keeping this mutable
  evidence outside the release source tree avoids changing the tree after the final build.

The wheel was extracted create-only and exercised from its extracted package rather than the
working tree. Version, CLI help, package UI self-test, create-only `init`, and zero-write
`doctor` all passed. The artifact smoke directories are ignored local evidence and are not
release content. Earlier create-only candidates remain preserved as historical evidence;
the candidate bound by that receipt supersedes them for operator review but does not itself
authorize publication. It adds no live endpoint, private key, or provider credential; its
configured support email and public encryption key are intentional public release metadata.

The Windows x64 portable ZIP was also extracted into a fresh create-only directory. Its
unsigned executable passed the packaged UI self-test, launched its frozen MCP child path for
a real composer-send self-test, initialized a fresh local workspace, and passed `doctor`.
The latest pre-freeze candidate is 17,879,235 bytes with SHA-256
`41efb305519cf58b9f94ff56ab08f6a128840726a70e02312c899ba7daaf18ef`; its SBOM SHA-256 is
`0f5e8beb7e96955630ed3acd31c1e0a72cfc102a1ccbbf8bb85b7e8b921ae3b2` and its exact
runtime-license manifest SHA-256 is
`7be5797e2f261e75bf40b8ff184680d5cb0306cd94718b3bb234e28e3996d857`.
The extracted executable passed encrypted-feedback, MCP-send, create-only initialization,
audit-doctor, direct zero-argument lifecycle, and command-launcher lifecycle checks. The
encrypted-feedback check emits a create-only receipt bound to the executable SHA-256,
product version, frozen runtime kind, and test identity; the verifier rejects a missing,
malformed, non-PASS, or mismatched receipt. Its passing receipt SHA-256 is
`75d26ff2378db7bcc5545935f077fea50e6eb9a290fd26b42e88c12af5196fb7`.
The first tagged local candidate was rejected by this check because Windows Git line-ending
conversion changed the public PEM from 625 to 636 bytes while its raw-byte SHA-256 pin still
described the pre-conversion file. The two public-key paths are now Git byte-stable (`-text`),
the pin binds the retained 636-byte files, and the Alpha 2 package passes without weakening
the exact raw-byte fingerprint comparison. The rejected Alpha 1 tag remains local audit
history and is not a release asset.
An earlier deep extraction placed `_cffi_backend` at a 251-character absolute path and the
Windows DLL loader rejected it as too long. The package now removes the redundant inner
`PeerBridgeControlRoom` directory, and the complete verifier passes from the same deep
create-only verification root. This is a portable-layout reliability fix, not a shortened
test-only path. The
Windows Store Python 3.13 build path is WMI-independent for the parent and every PyInstaller
isolated analysis worker; the previously redundant whole-package cryptography collection was
removed in favor of PyInstaller's maintained cryptography hook.
An independent read-only release review identified that the pre-freeze ZIP did not expose a
complete, version-bound runtime-license set at package root. The release packager now collects
the exact CPython, PyInstaller bootloader, cryptography, cffi, and Tcl/Tk license texts used by
each build, records component versions plus byte counts and SHA-256 values in
`THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json`, and binds the retained copy into portable
provenance. The portable verifier rejects missing, extra, duplicated, unversioned, misbound,
or hash-mismatched runtime-license entries. The current pre-freeze ZIP passes this gate, but
remains a dirty-source development candidate rather than the final tagged release asset.
GitHub Actions rebuilds the final portable ZIP from the frozen tag, verifies its expected
digest before extraction, retains that exact tested artifact, and publishes it with provenance
and checksums. The pre-freeze candidate hash is evidence, not a promise that the final tagged
artifact will have identical bytes.

## Security and privacy scan

- Wheel: no credential-shaped token, private key, or personal absolute path found.
- Source distribution: no private key, personal absolute path, `.peerbridge` runtime path, or
  unsafe package member found.
- Source and one-commit Git history: no personal absolute path, private key, sensitive tracked
  filename, or credential value found.
- The one source-distribution token-shape match is a benign long Python test identifier; it is
  not a key or fixture value.
- Core dependencies remain empty. `cryptography` is an explicit optional dependency used only
  for the opt-in encrypted feedback diagnostic path.
- Package license metadata is Apache-2.0 and includes the license file.

## Grok 4.6 adversarial review

One bounded relay exercise ran with an empty model-tool allowlist and zero tool rounds. The
model returned eight compact threat findings, made zero tool calls, and did not receive
credentials or private project data. The response content SHA-256 is
`1bf0038f3103d7577f4dcc580258794b8d4f7b97846ece696e091018e51370dc` and the sanitized
provider receipt SHA-256 is
`970148b061011945f0d250e7ccc7472b096126557bf81336be9719f1a196f0ae`.

The resulting fixes bind wheel/sdist payloads byte-for-byte to their source, reject plaintext
credential-shaped JSON/log/text Feedback attachments, and preserve a committed Cloudflare D1
intake after an ambiguous acknowledgement instead of deleting its R2 object or refunding its
quota. The focused Python security set and all 14 Cloudflare edge tests pass. Update checking
remains read-only and pinned to this repository's HTTPS GitHub release page. The optional
announcement feed is disabled unless configured, accepts bounded plain text and HTTPS links
only, and cannot invoke an Agent or execute update content; signed automatic installation is
explicitly outside this Alpha.

## Honest Alpha limitations

1. Physical Windows QA selected, staged, sent, and cleared one harmless text attachment in
   an automation-off disposable room. The visible local acknowledgement bound SHA prefix
   `7fabae9d11be15ae`; no external provider was invoked. Validation, staging, transport
   binding, tests, and UI self-test pass. Provider-side image understanding remains outside
   this Alpha.
2. Physical Windows QA confirms a full-sidebar leftward drag reaches the correct
   history-preserving removal confirmation, and a seat card exposes the live 39-model,
   3-provider catalog plus reasoning submenu. A dedicated regression proves the exact route
   survives Bridge restart and room switching. A later physical apply preserved the exact
   current Claude provider/model binding and is SHA-bound in the physical acceptance report.
   The operator subsequently completed and accepted the drag add/remove workflow. A later
   disposable acceptance committed `openai-official / gpt-5.6-luna / high`, then restored
   and committed `openai-official / gpt-5.3-codex-spark / high`, with both membership SHAs
   recorded in the physical acceptance report. Deterministic tests cover
   full-sidebar removal, remove dispatch, routed add dispatch, and durable route persistence.
   Separately, the installed official Codex client was queried without credentials or runtime
   hard-coding: all 7 visible models (including Sol, Terra, and Luna) were physically observed
   and produced 33 model/reasoning route choices, bound by
   `LOCAL_ALPHA_CODEX_CATALOG_ACCEPTANCE_20260815.md`.
3. Live root-post task `room-wakeup-e2e-20260815` obtained one parent-bound response each
   from Claude, Grok, and Kimi, recorded no reply fanout, and terminally marked the unavailable
   Codex route `configuration_invalid` before that unrunnable Seat was removed from the active
   Lobby. Agent-origin receipt `agent-root-wakeup-e2e-20260815-v1.json` separately proves that
   a Grok-originated root post woke Claude and Kimi exactly once without a reply cascade.
   A bounded live discussion was then run in `alpha-mcp-discussion-20260815123714`: Grok 4.6
   and Kimi were dispatched in parallel, both completed exactly once, both returned consensus,
   and the discussion terminally completed in its first round in about 35 seconds. Create-only
   receipt `.peerbridge/receipts/mcp-capability-discussion-e2e-20260815-v1.json` has file
   SHA-256 `8c3057cc4748788c9bfb44fa6dc14ebc79363a7113f5f16741c4d582f4e1425a`, embedded receipt
   SHA-256 `828b8bba51c045b5349a009370ec3840ff2ba8b16c3068edce04b2946f12cd76`, and re-verifies
   with zero writes. A later all-four-provider run remains an honest post-Alpha extension.
4. Remote/mobile control, managed cloud, paid remote service, native iPhone, signed Windows
   installer, and one-click signed updater are outside this local Alpha candidate.
5. Publication remains conditional on the final frozen-source gates and a current operator
   authorization; this evidence document alone does not authorize publication.

The detailed physical observation receipt is
`docs/LOCAL_ALPHA_PHYSICAL_UI_ACCEPTANCE_20260815.md`.
The operator-ready GitHub pre-release text is staged in
`docs/GITHUB_ALPHA_RELEASE_DRAFT_20260815.md`; it remains conditional on the final fresh
strict package/security run and current operator authorization.

## Final candidate binding rule

The final create-only wheel and source distribution are built only after this tracked evidence
text is frozen. Their exact paths, byte counts, SHA-256 values, source-tree SHA-256, test counts,
and command profile belong in `.peerbridge/receipts/local-alpha-release-final-v3.json`, which is
outside the release source tree. This avoids a circular rebuild in which writing artifact
hashes into tracked documentation changes the source that the artifacts are meant to bind.
The receipt does not authorize publication by itself; current operator authorization and all
final frozen-source gates are both required.
