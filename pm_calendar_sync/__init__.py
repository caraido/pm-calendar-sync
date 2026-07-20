"""
OKPM AppFolio → Google Calendar Sync  v2
==========================================
Polls AppFolio Plus Reports API (v2) and maintains one Google Calendar per
AppFolio PROPERTY GROUP.

─── Calendar model (group cutover, July 2026) ───────────────────────────────
  One calendar per property group from the property_group_directory report,
  summary = the group name VERBATIM (no " Portfolio" suffix — a group named
  after an owner must never summary-match that owner's retired calendar).
  Membership is data-driven: the PM edits groups in AppFolio and the sync
  follows.  A property in N groups appears on N calendars; promises are
  mirrored across them by the sibling machinery (formerly co-ownership).
  Properties in no group are intentionally unsynced.  Sharing is PM-ONLY
  (role=owner, so drag-to-promise works); group calendars are never shared
  with property owners — groups can span unrelated owners.
  The legacy per-owner calendars were retired in place at cutover:
  "[RETIRED] {name}" rename, non-PM ACLs stripped, events frozen as history.

─── Current-month model ─────────────────────────────────────────────────────
  All display is per DAY-GROUP: a tenant's same-day payments render as ONE
  event (sum in the title, each ledger row itemised in the description);
  state `payments` stays per ledger row.  NSF rows never merge into a group.

  STATUS EVENT  : one per month. Starts on the 1st (the "what's due" preview),
                  then migrates to the first payment date (absorbing that
                  whole day-group). Subsequent day-groups get their own
                  events; the status event stays put.
  PAYMENT EVENTS: one per day-group after the first, on each payment date.

  SETTLED-MONTH COLLAPSE (replaces the old grey muting; pre-deploy months
  keep their frozen grey events):
    The moment past_due <= 0, the month consolidates into ONE event — green
    (balance 0) / pink (credit) — on the LAST payment date; every payment
    event is deleted and the settled event's description itemises the full
    payment history and promise history.  A later advance payment re-anchors
    it (stays collapsed).  Per-month state machine (month entry
    collapse_state, owned by transforms.resolve_collapse_transition):
      COLLAPSED   → the settled single-event display above.
      FROZEN      : a charge posts after settlement with no new payment —
                    the settled event is left untouched (the balance shows
                    up in next month's due amount instead).
      REACTIVATED : the tenant pays toward that post-settle charge in the
                    same month — the settled event morphs in place into a
                    normal status event tracking ONLY the new payments
                    (fresh running balances); the settled payments live on
                    as a "Previously settled" description section, never as
                    events again.  Fully paid again → collapses again.
      REVERT      : a settled payment vanishes from the ledger or comes back
                    NSF-flagged while a balance is owed (the classic
                    days-later NSF bounce) — the collapse undoes itself: the
                    expanded view is rebuilt (settling event yellow again,
                    every remaining payment event restored) and the bounced
                    payment shows red (its surviving row renders NSF, or a
                    reconstructed "ghost" event when the row vanished).  A
                    reversal against an already-rolled-over settled month
                    un-collapses that prior month from stored state.

─── Future-month model ──────────────────────────────────────────────────────
  PLACEHOLDER   : frozen event on the 1st. Unfrozen only for next month when
                  the current tenant has a credit balance.

─── Commitments (promise-to-pay) ────────────────────────────────────────────
COMMITMENT / PROMISE EVENTS
  The PM registers a payment plan by dragging an event to a future date in
  Google Calendar.  The next poll detects the move and converts the event in
  place into an  okpm_event_type = "commitment"  event.  The commitment keeps
  the red/yellow colour of its live balance (🔴 nothing paid this month,
  🟡 partial payment outstanding).

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
       Display recomputes from the live balance: 🔴 when nothing has been paid
       this month, 🟡 when a partial payment leaves a balance.  A missed promise
       (date already passed) is NOT specially flagged — it keeps its 🔴/🟡 colour
       and stays on its date until renegotiated or paid (no auto-expire).
    3. Absorbed: ANY (non-NSF) payment dated on a promise's live anchor date
       deletes that one promise, regardless of amount — the tenant kept the
       date.  Only payments dated today or earlier count (a post-dated row
       has not "kept" anything yet), and a promise created/converted this
       very run survives at least one full poll.  Other promises stay.
       Never kickstarts.  Recorded as outcome "kept" in the month's
       promise_history.  (Full/update only — submit has no ledger; the next
       sweep absorbs.)
    4. Resolved: every promise for the unit is deleted when balance ≤ 0 (full
       payment), recorded as outcome "resolved".  The settled event's
       "Promise history" section lists both outcomes.  Eviction handling is a
       separate track for later.
    5. Safe delete (≥1-promise rule): the PM deleting a promise sticks only while
       another promise remains (rearranging installments).  Deleting the LAST
       promise is treated as a slip and one is recreated, so a tracked unit keeps
       at least one promise until paid.  An ABSORBED promise is exempt: keeping
       the last promised date may legitimately leave a still-owing unit with
       zero promises (the PM drags a new one when renegotiated).

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

  PM ACCESS: Owner role, so the PM can drag events and the calendar lands
  under "My calendars".  Locked events are detect-and-reverted within one
  poll cycle.

STATE ADDITIONS:
  state.json["_commitments"][soid] = list of:
    { event_id, anchor_date, source_type, origin_month, covers_rent_month,
      calendar_id }
  soid = "{occupancy_id}@g{property_group_id}" (the "g" prefix keeps group
  scopes disjoint from the purged legacy "{occupancy_id}@{owner_id}" keys);
  month keys are "{soid}_{YYYY-MM}".
  Month entries (fmt=2, since the settled-collapse deploy) additionally hold:
    fmt=2                — day-group/collapse format marker (any other value
                           gets one forced regroup+collapse rebuild)
    collapse_state       — None | "collapsed" | "frozen" | "reactivated"
    collapse_baseline    — number of settled rows retired into history
    settled_rows         — snapshot of those rows (same shape as payments)
    settled_past_due     — past_due at (re-)collapse (0 or the credit)
    settled_on           — ISO date the month (re-)settled
    promise_history      — [{event_id, anchor_date, source_type,
                             origin_month, covers_rent_month,
                             outcome: "kept"|"resolved", recorded}]
  payment_event_ids / payment_event_dates are per DAY-GROUP; payments stays
  per ledger row.
  state.json["_calendars"]["g{gid}"]        = group calendar id
  state.json["_retired_calendars"][owner_id] = legacy owner calendar id
  state.json["_migrations"]["group_cutover_v1"] = one-time cutover marker
  (gates the cutover in run(); update/submit no-op until it exists)

New GitHub variable:
  COMMITMENT_LOOKAHEAD_MONTHS  (default 3) — how many future months to scan
  each run for moved placeholders.  Add to sync.yml vars section.
"""

from .orchestrator import SyncOrchestrator

__all__ = ["SyncOrchestrator"]
