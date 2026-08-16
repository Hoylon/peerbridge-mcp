# PeerBridge Local Alpha Physical UI Acceptance

Date: 2026-08-15 (Asia/Taipei)

This create-only receipt records physical Windows UI observations against the running
`PeerBridge MCP Control Room // LIVE` window. It does not publish the package, alter provider
credentials, or invoke an external model. Later observations below used only a disposable
local room with automation disabled.

## Observed passes

1. **Leftward room-Agent drag**
   - Dragging the visible `grok-relay` room card onto the full left Global Agent Library rail
     opened the expected `Remove Room Seat` confirmation.
   - The dialog explicitly stated that existing message history would not be deleted.
   - The removal was cancelled, so room membership and message history remained unchanged.

2. **Per-seat provider/model menu**
   - The `claude-code` card's down-arrow opened its seat route menu.
   - The live menu reported `39 models / 3 Providers`.
   - Opening one discovered relay provider displayed its 13-model Claude catalog, including
     Fable, Haiku, Opus, and Sonnet variants.
   - Opening a model submenu displayed its provider-default reasoning choice.
   - No route or reasoning choice was committed, so the existing seat route remained intact.

3. **Current-route physical apply and persistence check**
   - In a later create-only observation at `2026-08-15T03:20:52Z`, the `claude-code` card was
     selected and the currently displayed route was applied through the visible
     `+ Apply Seat` control without changing provider, model, or reasoning.
   - The live database remained bound to route
     `ccswitch-claude-d990f03e13bd-2858afd746`, provider
     `ccswitch-claude-d990f03e13bd`, and model `claude-fable-5`.
   - The resulting membership SHA-256 is
     `d8ce9a33deb66c2ab631ab2c3b2cbe95ca20923e04126e0aadb72a681c4aec22`; the route
     profile SHA-256 is
     `7c4bc1483ac8e71037065bfedb1ee7899c5c02fa322831263943919f0b39a0d1`.
   - This proves the physical apply path preserves the exact current binding. It does not
     replace the remaining acceptance for choosing a different disposable model/reasoning
     route and restoring it.

4. **Desktop attachment chooser**
   - The composer attachment button opened the native Windows file chooser.
   - The chooser used the expected safe image/text filter.
   - The chooser was cancelled before selecting a file; no attachment was staged or sent.

5. **Room-Agent add/remove workflow**
   - The operator subsequently completed the real drag-to-add and drag-to-remove workflow
     and explicitly confirmed that it works on the frozen desktop geometry.
   - Existing message history remained intact, matching the history-preserving removal
     contract.

6. **Refresh continuity and language discovery**
   - The operator physically confirmed that refresh no longer flashes or visibly rebuilds
     the page.
   - The running candidate shows a quiet localized successful-refresh timestamp below the
     refresh control, so an unchanged refresh still has visible acknowledgement.
   - The locale selector is preceded by the permanent English label `Language`, allowing an
     English reader to find the language control before changing locale.

7. **Disposable attachment send and clear**
   - A disposable room `alpha-ui-acceptance-20260815` was created with automation set to
     `off`, containing only `human-operator` and `codex-main`.
   - The native Windows chooser selected the harmless local text file
     `.peerbridge/ui-acceptance/attachment-smoke.txt`.
   - The composer visibly staged `attachment-smoke.txt`, sent one local message, and then
     cleared the staged attachment without invoking any external provider.
   - The visible terminal acknowledgement bound the message to SHA prefix
     `7fabae9d11be15ae` at `2026-08-15 22:56:45` local time.

8. **Official Codex catalog, alternate route, and restore**
   - The `codex-main` card menu physically reported `26 models / 2 Providers`.
   - The official provider submenu physically displayed all seven discovered models:
     `gpt-5.3-codex-spark`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.5`, `gpt-5.6-luna`,
     `gpt-5.6-sol`, and `gpt-5.6-terra`.
   - The UI committed the disposable alternate route `openai-official / gpt-5.6-luna /
     high`; its membership SHA-256 was
     `00ed7999943b99bfcb11232ec864ab8a6e7c8a70cdf89e8835ca27c786fbd709`.
   - The UI then restored and committed `openai-official / gpt-5.3-codex-spark / high`.
     The final active membership SHA-256 is
     `3c5908920ce8ba26da28081043232e9c3e302a9b0d925e1ae3bf9feb9e8f468b`.
   - No prompt was posted while either route was selected, so this acceptance did not spend
     provider tokens or claim an inference receipt.

9. **Bounded discussion controls**
   - In the same disposable room, the automation selector visibly offered `off`, one reply
     per Agent, and bounded discussion.
   - Bounded discussion applied successfully and exposed the configured round, message, and
     stagnation limits plus pause/resume/continue/stop controls.
   - The room was restored to automation `off` without posting a message.

## Remaining physical acceptance

- Repeat visual-fit checks when changing supported Windows scaling or display geometry. The
  current desktop geometry has passed; changing global Windows display settings is not part of
  this create-only receipt.

The automated test suite, package UI self-test, and strict package gates remain the
authoritative non-physical evidence. This receipt records only the physical Windows surface.
