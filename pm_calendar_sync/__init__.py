"""
OKPM AppFolio → Google Calendar Sync  v2
==========================================
Polls AppFolio Plus Reports API (v2) and maintains per-owner Google Calendars.

─── Current-month model ─────────────────────────────────────────────────────
  STATUS EVENT  : one per month. Starts on the 1st (the "what's due" preview),
                  then migrates to the first payment date (absorbing that
                  payment). Subsequent payments get their own events; the status
                  event stays put.
  PAYMENT EVENTS: one per payment after the first, on each payment date.

─── Future-month model ──────────────────────────────────────────────────────
  PLACEHOLDER   : frozen event on the 1st. Unfrozen only for next month when
                  the current tenant has a credit balance.

─── Commitments (promise-to-pay) ────────────────────────────────────────────
COMMITMENT / PROMISE EVENTS
  The PM registers a payment plan by dragging an event to a future date in
  Google Calendar.  The next poll detects the move and converts the event in
  place into an  okpm_event_type = "commitment"  event (tangerine color).

  MOVABLE events (PM may drag; a forward move registers a promise):
    • Status events               "status"   — the 1st-of-month event dragged
    • Payment events              "payment"  — a logged payment dragged
    • Future-month placeholders   "rent"     — kickstart commitment
  (Historical "late"-sourced commitments, created before the today-marker
   dashboard was retired, are still tracked and managed normally.)

  LOCKED events (snapped back within one poll if accidentally moved):
    • Status / payment events not (yet) dragged into a promise

  COMMITMENT lifecycle:
    1. Detected (event dragged to a future date → converted in-place).
    2. Updated each run: auto section rebuilt, PM notes above divider preserved.
       Display recomputes from the live date + balance: 🔴/🟡 while on or after
       today, ⚠️ Overdue once the promised date has passed unpaid (no auto-expire
       — a missed promise stays on its date until renegotiated or paid).
    3. Resolved: every promise for the unit is deleted when balance ≤ 0 (full
       payment).  Eviction handling is a separate track for later.
    4. Safe delete (≥1-promise rule): the PM deleting a promise sticks only while
       another promise remains (rearranging installments).  Deleting the LAST
       promise is treated as a slip and one is recreated, so a tracked unit keeps
       at least one promise until paid.

  SPLIT PAYMENT PLANS:
    PM copy-pastes a commitment event for multiple promise dates. Each copy
    is discovered via extended-property listing and tracked independently.
    PM edits the "PROMISED:" line above the divider per event.

  KICKSTART COMMITMENT SUPPRESSION:
    While a kickstart commitment covers month M, the placeholder on the 1st is
    not recreated. When M becomes current with no payments yet, no status event
    is created until the first payment arrives; the commitment anchors the month.

  COMMITMENT CROSSING MONTHS:
    A commitment dragged into a future month pre-loads that month's rent in the
    displayed outstanding. When that month becomes current, the kickstart
    placeholder is deleted; the commitment anchors the month until paid.

  PM ACCESS: Owner (was reader) so the PM can drag events; owners stay reader.
  Locked events are detect-and-reverted within one poll cycle.

STATE ADDITIONS:
  state.json["_commitments"][oid] = list of:
    { event_id, anchor_date, source_type, origin_month, covers_rent_month }

New GitHub variable:
  COMMITMENT_LOOKAHEAD_MONTHS  (default 3) — how many future months to scan
  each run for moved placeholders.  Add to sync.yml vars section.
"""

from .orchestrator import SyncOrchestrator

__all__ = ["SyncOrchestrator"]
