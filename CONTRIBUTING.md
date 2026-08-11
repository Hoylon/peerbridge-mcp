# Contributing

PeerBridge MCP welcomes focused bug reports, documentation improvements, interoperability
tests, and narrowly scoped security hardening.

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

Linux and macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

## Pull requests

1. Open or reference an issue for behavioral changes.
2. Keep changes small enough to review in one pass.
3. Add regression tests for state, cursor, lease, path, review, or audit behavior.
4. Run `python -m pytest`, `python -m compileall -q src`, and `python -m build`.
5. Confirm no database, WAL, credentials, private chat, or local path was added.
6. Explain compatibility impact for Codex, Claude Code, and MCP protocol revisions.

Do not submit code copied from a repository with an incompatible license or additional
usage restrictions. Cite conceptual inspirations in the pull request when relevant.

## Design principles

- Equal peers, one writer per task.
- Local-first and inspectable.
- Fail closed on ambiguous path ownership or stale proof.
- No automatic patch application.
- No hidden model invocation or credential forwarding.
- Every state transition should be attributable and hash-bound.
- Backward compatibility must be tested, not assumed.

## Commit messages

Use an imperative summary such as `Add dual-era MCP discovery support`. Put test evidence
and migration notes in the commit body when the change affects stored state.
