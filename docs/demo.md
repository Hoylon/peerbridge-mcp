# Maintainer Showcase

The showcase creates only synthetic files, adapter identities, and coordination records.
It does not contact or authenticate to any model provider.

```powershell
python examples\demo_workflow.py --workspace demo-workspace
peerbridge doctor --project-root demo-workspace --scope demo
peerbridge monitor --project-root demo-workspace --scope demo
```

Expected workflow:

1. `codex-demo`, `claude-demo`, `grok-demo`, and `kimi-demo` publish distinct synthetic
   adapter identities and join one maintenance room.
2. Codex claims one exact write path.
3. Claude attempts to claim the same path and is rejected by the live lease conflict check.
4. Codex records the before/after hashes and requests independent reviews.
5. Claude and Grok submit approvals, satisfying a two-of-three quorum.
6. Codex completes the task only after PeerBridge rehashes the file.
7. The human operator sends a room-bound audited status message.
8. The audit verifier reports a valid chain and performs zero writes.

The monitor should show:

- four Agent seats with separate adapter labels;
- a completed workboard row;
- two peer reviews;
- one proof record;
- the message and event history.

The JSON receipt contains no credential or lease capability. `synthetic: true` is a required
field so the result cannot be presented as real provider inference. See
[Technical Showcase](technical-showcase.md) for the architecture and evidence map.

Use a disposable workspace. `demo-workspace` is ignored by Git.
