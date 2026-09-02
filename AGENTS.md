# PeerBridge Agent Instructions

- Read `DESIGN.md` before changing any local, remote-desktop, or mobile interface.
- Preserve the same room, task, Agent, dispatch, audit, and permission semantics on every
  viewport and transport.
- A visible command must call a real bounded backend operation. Never add decorative send,
  interrupt, approval, file, voice, shell, or power controls.
- Tailscale Serve is the production browser transport. The Tailcat integration is visible
  and enabled by default; its managed Port, SSH, and Exit node process must remain
  client-key allow-listed, locally switchable, and independently authenticated. A Tailcat
  address is transport discovery, not PeerBridge authorization.
- Keep credentials, local context imports, provider diagnostics, and project databases out
  of Git and rendered snapshots.
- Run focused tests, the full suite with a fresh temporary root, desktop/mobile visual
  checks, and a secret scan before publication.
