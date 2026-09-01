# PeerBridge Interface Design Contract

PeerBridge is an operational multi-agent control room. It must feel quiet,
precise, and trustworthy under repeated use. This document is the source of
truth for desktop, remote-desktop, and mobile presentation.

## Product Shape

- Open on the usable control room, never a marketing landing page.
- Desktop uses a persistent room/project rail, a central conversation or work
  timeline, and a bounded inspector/control surface.
- Mobile uses a drawer for rooms and projects, a compact Conversation / Work
  mode switch, and a fixed bottom composer. The same room, audit state, and
  permissions must be visible on every viewport.
- Keep one canonical information hierarchy across local WebView, Tailscale
  remote, and experimental Tailcat transports. Transport must not change what
  an action means.

## Visual System

- Use near-black, neutral charcoal, white, and restrained cool blue. Amber,
  green, and red are semantic status colors only.
- Use system UI fonts for interface text and a monospace font only for IDs,
  hashes, timestamps, and terminal output.
- Keep radius at 8px or less. Do not place cards inside cards or turn every
  section into a floating tile.
- Use 1px borders, stable row heights, and clear alignment. Avoid gradients,
  decorative glows, or oversized headings.
- Icons must come from the established icon set where available. Every icon-only
  command requires an accessible label and tooltip.

## Conversation And Work

- Conversation rows show speaker, destination, time, route, and delivery state
  without exposing credentials or hidden reasoning.
- Tool and dispatch activity uses one consistent lifecycle: queued, running,
  completed, retryable, failed, cancelled. Show concise evidence such as target,
  attempt count, and timestamp.
- A live call exposes a familiar stop control. Stopping must invoke an existing
  bounded, audited backend operation; never display a decorative control that
  cannot interrupt work.
- The composer remains reachable while scrolling. Attachments, voice, and send
  are commands, not navigation tabs.
- Empty, loading, reconnecting, permission-denied, failed, and completed states
  are first-class views and must not resize the surrounding layout.

## Responsive Rules

- Wide desktop: three functional columns when space permits.
- Narrow desktop/tablet: collapse the inspector before the room rail.
- Mobile: hide the rail behind a drawer, keep the mode switch and health state in
  the top bar, and pin the composer above the safe-area inset.
- Touch targets are at least 44px. Long IDs and model names wrap or truncate with
  a title; they must never force horizontal page scrolling.
- Verify at 1440x900, 1024x768, 390x844, and 360x800.

## Security And Remote Boundaries

- Tailscale Serve remains the production remote path. It requires tailnet
  identity plus the independent PeerBridge launcher credential.
- Tailcat is opt-in and experimental. Its token is transport discovery, not
  PeerBridge authorization. File transfer, forwarding, SSH, SOCKS5, and exit-node
  modes remain separately enabled and separately described.
- Remote controls expose only explicitly implemented, audited actions. Never
  infer that access to the interface grants shell, file, provider-secret, review,
  or power-control authority.
- Redact credentials before rendering and keep raw logins, access tokens, and
  provider secrets out of snapshots, logs, evidence, and Git.

## Release Acceptance

- Run backend and frontend tests with a fresh temporary root.
- Render and inspect desktop and mobile screenshots; reject blank windows,
  overlapping controls, clipped text, missing taskbar icons, or horizontal body
  scroll.
- Recheck packaged icon resources and the Windows AppUserModelID path.
- Secret-scan every tracked and newly added file before publication.
- Experimental transports must be labeled as such and may not satisfy the
  production remote/mobile release gate.
