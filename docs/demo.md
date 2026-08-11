# Demo Walkthrough

The demo creates only synthetic files and coordination records.

```powershell
python examples\demo_workflow.py --workspace demo-workspace
peerbridge doctor --project-root demo-workspace --scope demo
peerbridge monitor --project-root demo-workspace --scope demo
```

Expected workflow:

1. `codex-demo`, `grok-demo`, `deepseek-demo`, and `kimi-demo` publish presence.
2. Codex claims `demo-change` with a write lease on `src`.
3. Codex records a synthetic file proof and requests independent reviews.
4. Grok and DeepSeek submit approvals, satisfying a two-of-three quorum.
5. Codex completes the task after PeerBridge rehashes the file.
6. The human operator sends an audited status message.
7. The audit verifier reports a valid chain.

The monitor should show:

- four agent tiles;
- a completed workboard row;
- one peer review;
- one proof record;
- the message and event history.

Delete the synthetic `demo-workspace` when finished. It is ignored by Git.
