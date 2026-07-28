# Console page overrides

Inherits `../MASTER.md`.

## Layout
- Two-column operator console: conversation + engagement/activity rail
- **Full-bleed workspace** — no centered `max-width` cap; horizontal padding `12–16px` only (dense dashboard / density 8)
- **Equal-height columns** — `.shell` fills `100dvh` / `100%`; `.workspace` is `1fr` row with `align-items: stretch` so Engagement matches Conversation height
- Grid: `minmax(0, 1fr) minmax(620px, 1.2fr)` — Engagement slightly wider (~55% on large screens, min 620px)
- Mid breakpoints: `≤1400` min 540px / `1.15fr`; `≤1200` min 480px / `1.1fr`; stack at `≤1024px`
- Chat + Activity fill remaining panel height (`flex: 1`); no fixed `560px` caps on desktop
- Activity open state uses flex column; `#activityLines` scrolls inside
- Sticky top bar with brand mark + run status chip
- Live activity remains a dark terminal surface for stream contrast
- Keep chat bubbles readable (`max-width` on bubbles / ~65–75ch), not the whole workspace

## Density
- Compact panel headers (mono uppercase)
- 8–12px internal rhythm; avoid oversized marketing hero treatment
- Dense dashboard dial (path forms + activity) without crowding conversation bubbles

## Live activity
- Keep **all runs** in one panel (do not wipe on each request)
- Append committed lines to engagement-root `activity.log` (and `.dbt_agent/activity.log`) — plain text matching Activity History run blocks; thinking snapshotted when each brain step / request ends
- Activity History UI is session/memory only — does not reload the log on Open or restart
- Live model stream stays in memory only while streaming; final thinking snapshot is written to the log
- Wipe UI history when engagement is Opened (new session); `activity.log` is left intact (append-only)
- Conversation cookie history clears on reload / Open (not restored from session)
- Accessibility: composer has sr-only label; errors use `role=alert` + assertive live region; Activity toggle is always available (outside busy bar); skip link to conversation; status chip is `aria-live=polite`
- Section each request as `Run · Chat|Brain|Connection|…`
- Current run header highlighted; prior runs remain scrollable above
- Thinking stream snapshots into the run when the model finishes
- Outer panel + nested detail panes both pin to latest logs while a run is active (`#activityLines` is the scroll container)
- Opening Activity or receiving new live/busy events moves the scrollbar to the bottom; scrolling up to read history is kept until you return near the bottom
