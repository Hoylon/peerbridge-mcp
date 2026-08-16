# Local Alpha Codex Catalog Acceptance (2026-08-15)

This receipt records a live, credential-free model-catalog query against the installed official
Codex client. PeerBridge invoked `codex debug models` without a shell, parsed only entries whose
official visibility is `list`, and generated route choices from the returned model and reasoning
metadata. No model names or reasoning levels in this receipt were used as runtime fallbacks.

## Live result

- Visible official models: **7**
- Generated model/reasoning route choices: **33**
- Parsed catalog SHA-256:
  `5f1e8c97e3e9399646e4e06d3ca94ba12915f8aefff6e7d0f4de6dcfbcaa5bc9`
- Discovery and route generation completed successfully with no credential values read or logged.
- Focused catalog parsing, menu generation, and restart/room-switch persistence checks:
  **9 passed** using a project-local isolated pytest base directory.

| Model | Reasoning choices reported by the installed client |
| --- | --- |
| `gpt-5.6-sol` | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-terra` | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-luna` | low, medium, high, xhigh, max |
| `gpt-5.5` | low, medium, high, xhigh |
| `gpt-5.4` | low, medium, high, xhigh |
| `gpt-5.4-mini` | low, medium, high, xhigh |
| `gpt-5.3-codex-spark` | low, medium, high, xhigh |

## Acceptance boundary

This closes the dynamic-discovery and no-hard-coding portion of D-12 for the current installed
client. The remaining physical acceptance is to open the Codex submenu in the frozen desktop
build, confirm these seven current models are presented, commit one disposable route, and verify
the observed provider/model identity. Catalog changes in a future client are expected and must be
discovered again rather than copied from this receipt.
