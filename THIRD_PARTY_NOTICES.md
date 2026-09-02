# Third-Party Notices

PeerBridge MCP source code is licensed under Apache-2.0 as described in `LICENSE`.
The PeerBridge names and logo assets are governed separately by `TRADEMARKS.md` and
`BRAND_ASSETS.md`.

The core Python package has no mandatory third-party runtime dependencies. Optional,
development, edge-support, and Windows portable tooling can use the following projects:

| Component | Use | Upstream license |
| --- | --- | --- |
| Python | Interpreter; included in Windows portable builds | Python Software Foundation License 2.0 |
| cryptography | Optional encrypted feedback export and Windows builds | Apache-2.0 OR BSD-3-Clause |
| PyInstaller | Windows portable build tooling and bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception |
| setuptools | Python build backend | MIT |
| build | Python build frontend | MIT |
| wheel | Python wheel tooling | MIT |
| pytest | Test tooling | MIT |
| pytest-cov | Test coverage tooling | MIT |
| Wrangler | Optional Cloudflare Worker development and deployment tooling | MIT OR Apache-2.0 |
| Tailcat | Optional on-demand encrypted CLI transport, downloaded from a pinned official release | BSD-3-Clause |

The Windows portable package contains an SPDX 2.3 file inventory named
`SBOM.spdx.json` and a `THIRD_PARTY_LICENSES/LICENSES_MANIFEST.json` inventory. The
runtime-license inventory is generated from the exact build interpreter, bundled runtime,
and build-tool distributions; it binds every copied license text by component, version,
byte count, and SHA-256. The corresponding package distributions and upstream repositories
remain authoritative for source-level attribution and transitive dependency terms.

GitHub Actions used by this repository are pinned in CI and are build infrastructure, not
distributed application components. The Tailcat binary is not committed or bundled; when
the default-on integration needs it, PeerBridge downloads one pinned official archive,
verifies its archive and executable SHA-256, and retains the upstream license and README in
local private state. Tailscale services, Cloudflare services, provider CLIs, Codex, and
Claude Code remain external integrations.
