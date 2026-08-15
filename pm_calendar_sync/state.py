"""State persistence (state.json) and the commitment registry."""
import json
import re
from datetime import datetime
from typing import Optional

from .config import STATE_FILE, log


class StateManager:
    """
    Per occupancy+month  (fmt=2 — day-groups + settled collapse):
      fmt,                   — 2; any other value marks a pre-collapse entry
                               and forces one regroup+collapse rebuild
      status, past_due,
      status_event_id, status_event_date,
      late_event_id,
      payment_event_ids,     — one id per DAY-GROUP (same-day payments render
                               as a single event), index 1+ of the groups
      payment_event_dates,   — index-aligned canonical dates for
                               payment_event_ids; submit-mode payment-drag
                               detection reads them (no ledger there)
      payments,              — [{date, amount, is_nsf, description}] for ALL
                               of the month's payments, one entry per LEDGER
                               ROW (never grouped); NSF-reversal
                               reconciliation and the settled-baseline
                               integrity checks match against these
      collapse_state,        — None/absent = expanded | "collapsed" |
                               "frozen" | "reactivated" (settled-month
                               collapse; transforms.resolve_collapse_transition
                               owns the transitions)
      collapse_baseline,     — number of settled rows retired into the
                               settled event's description history
      settled_rows,          — snapshot of those rows (same shape as
                               payments); baseline-integrity checks compare
                               them against the live pull
      settled_past_due,      — past_due at (re-)collapse (0 or the credit)
      settled_on,            — ISO date the month (re-)settled
      promise_history,       — [{event_id, anchor_date, source_type,
                               origin_month, covers_rent_month,
                               outcome: "kept"|"resolved", recorded}] —
                               kept = absorbed by a same-day payment,
                               resolved = balance cleared; rendered as the
                               settled event's "Promise history" section
      nsf_reversals_applied, — [{key, ref, date, amount, v}] reversals
                               already applied to this soid's month
                               (per-calendar idempotence markers; v=2 means
                               the full flip incl. Status-line rewrite —
                               older markers trigger one retouch pass; the
                               month's own status is deliberately left as
                               written, the marker and event notes are the
                               honest annotation)
      nsf_event_ids,         — payment events flipped to NSF display (or
                               reconstructed "ghost" events) after their
                               ledger row vanished; cleared when a month
                               collapses (their events are consolidated)
      last_updated

    Future-month entries additionally use:
      rent_event_id     — placeholder event ID
      is_commitment     — True when the placeholder was converted to a commitment

    Top-level commitment registry  (new in v2):
      state.data["_commitments"][soid] = [
        {
          event_id        : str,   Google Calendar event ID
          anchor_date     : str,   ISO date where PM anchored the event
          source_type     : str,   "status" | "payment" | "late" | "kickstart"
          origin_month    : str,   YYYY-MM of the original event's month
          covers_rent_month: str|None  YYYY-MM if commitment crosses into a
                                       future month and pre-loads that rent
          calendar_id     : str,   Google Calendar ID this commitment lives on
        },
        ...   # one entry per split (copy-pasted events)
      ]

    Top-level calendar-id map  (cache for fast modes):
      state.data["_calendars"][scope_key] = calendar_id
      scope_key is "g{property_group_id}" (e.g. "g3") since the group
      cutover; before it, keys were bare owner_ids.  Written whenever a
      calendar is resolved; read by non-nightly modes to skip the
      calendarList() pagination.  The nightly full sweep re-verifies every
      calendar live (by id) and rewrites the map.

    Group-cutover bookkeeping (July 2026):
      state.data["_retired_calendars"][owner_id] = calendar_id
        The legacy per-owner calendars, moved out of _calendars at cutover.
        Kept for traceability and as input to misc/rollback_group_cutover.py.
      state.data["_migrations"][key] = {..., "done_at": iso}
        One-time migration markers; "group_cutover_v1" gates the cutover in
        run() and the fast-path bail-outs in run_update()/run_submit().

    Departed-occupancy lifecycle (Aug 2026):
      state.data["_departed_pending"][bare_oid] = {
        first_missing_at : ISO date of the first live-roll run missing it
        last_row         : the oid's final rent_roll row (from the previous
                           snapshot) — None when it wasn't captured in time
        unit_id, scopes  : attribution hints for the cleanup
        backlog          : True for pre-deploy departures (breaker-exempt,
                           gated by the departed_backlog_v1 migration)
      }
        Flagged on any live-roll run; cleared if the oid reappears.  The
        nightly confirms each against BOTH live reports (rent_roll AND
        tenant_directory) before cleaning.
      state.data["_departed"][bare_oid] = {cleaned_at, move_out_date,
        scopes, months_purged, events_deleted, events_kept,
        marker_event_ids}
        Post-cleanup audit — counts only, never notes; git history of
        state.json is the archive of the purged month entries.

    Soids (state month keys "{soid}_{YYYY-MM}" and _commitments keys) are
    scoped "{occupancy_id}@g{group_id}" since the cutover; legacy
    "{occupancy_id}@{owner_id}" entries were purged by it (git history of
    state.json is the archive).
    """

    def __init__(self):
        self.path = STATE_FILE
        self.data: dict = {}
        if self.path.exists():
            # Decode defensively against PowerShell encoding mishaps.  `echo >`
            # writes UTF-16LE (BOM ff fe); `Set-Content` can write UTF-8-with-BOM
            # (ef bb bf).  Plain utf-8 decoding chokes on either ("invalid start
            # byte 0xff" / a stray BOM char).  Sniff the BOM and decode to match.
            raw = self.path.read_bytes()
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                text = raw.decode("utf-16")          # UTF-16 LE / BE
            elif raw.startswith(b"\xef\xbb\xbf"):
                text = raw.decode("utf-8-sig")        # UTF-8 with BOM
            else:
                text = raw.decode("utf-8")            # plain UTF-8
            self.data = json.loads(text) if text.strip() else {}
        # Ensure commitment registry and calendar-id map exist
        if "_commitments" not in self.data:
            self.data["_commitments"] = {}
        if "_calendars" not in self.data:
            self.data["_calendars"] = {}
        if "_retired_calendars" not in self.data:
            self.data["_retired_calendars"] = {}
        if "_migrations" not in self.data:
            self.data["_migrations"] = {}
        if "_departed_pending" not in self.data:
            self.data["_departed_pending"] = {}
        if "_departed" not in self.data:
            self.data["_departed"] = {}

    def _key(self, oid: str, month: str) -> str:
        return f"{oid}_{month}"

    def get(self, oid: str, month: str) -> Optional[dict]:
        return self.data.get(self._key(oid, month))

    def set(self, oid: str, month: str, entry: dict):
        entry["last_updated"] = datetime.utcnow().isoformat()
        self.data[self._key(oid, month)] = entry

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    # ── Calendar-id map ───────────────────────────────────────────────────────

    def get_calendar_id(self, scope_key) -> Optional[str]:
        return self.data["_calendars"].get(str(scope_key))

    def set_calendar_id(self, scope_key, calendar_id: str):
        self.data["_calendars"][str(scope_key)] = calendar_id

    # ── Migration bookkeeping (group cutover) ─────────────────────────────────

    def migration_done(self, key: str) -> bool:
        return key in self.data["_migrations"]

    def mark_migration_done(self, key: str, info: dict = None):
        entry = dict(info or {})
        entry["done_at"] = datetime.utcnow().isoformat()
        self.data["_migrations"][key] = entry

    def retire_calendar_entry(self, owner_id):
        """Move a legacy owner entry out of _calendars into _retired_calendars
        so fast paths can never resolve it again but rollback tooling can."""
        cal_id = self.data["_calendars"].pop(str(owner_id), None)
        if cal_id:
            self.data["_retired_calendars"][str(owner_id)] = cal_id

    _LEGACY_MONTH_KEY = re.compile(r"^\d+@\d+_\d{4}-\d{2}$")
    _LEGACY_COMM_KEY  = re.compile(r"^\d+@\d+$")

    def purge_legacy_owner_entries(self) -> int:
        """Delete owner-scoped month entries and _commitments keys left over
        from before the group cutover.  Only call after a fully successful
        cutover — the migrated replacements are the "@g"-scoped keys.  Bare
        (unscoped) keys are untouched: migrate_bare_commitments still owns
        those.  Returns the number of entries removed."""
        dead_months = [k for k in self.data
                       if self._LEGACY_MONTH_KEY.match(k)]
        for k in dead_months:
            del self.data[k]
        dead_comms = [k for k in self.data["_commitments"]
                      if self._LEGACY_COMM_KEY.match(k)]
        for k in dead_comms:
            del self.data["_commitments"][k]
        purged = len(dead_months) + len(dead_comms)
        if purged:
            log.info(f"  Purged {len(dead_months)} legacy month entr(y/ies) "
                     f"and {len(dead_comms)} legacy commitment key(s)")
        return purged

    # ── Departed-occupancy helpers ────────────────────────────────────────────

    _SOID_MONTH_KEY = re.compile(r"^(\d+)@(g\d+)_(\d{4}-\d{2})$")
    _SOID_COMM_KEY  = re.compile(r"^(\d+)@(g\d+)$")

    def scoped_months(self, soid: str) -> list[tuple[str, str]]:
        """[(month, full_key)] for one soid's month entries, sorted by month
        (prefix scan over the flat key space)."""
        prefix = f"{soid}_"
        out = [(k[len(prefix):], k) for k in self.data
               if k.startswith(prefix) and self._SOID_MONTH_KEY.match(k)]
        out.sort()
        return out

    def purge_soid_months(self, soid: str) -> int:
        """Delete every month entry of one soid (the events were already
        handled by the caller); returns the count.  Git history of
        state.json is the archive — same contract as
        purge_legacy_owner_entries."""
        keys = [k for _, k in self.scoped_months(soid)]
        for k in keys:
            del self.data[k]
        return len(keys)

    def known_occupancy_scopes(self) -> dict[str, set]:
        """bare oid → {"g2", ...} from month keys AND _commitments keys —
        every occupancy this state file still tracks anywhere."""
        scopes: dict[str, set] = {}
        for k in self.data:
            m = self._SOID_MONTH_KEY.match(k)
            if m:
                scopes.setdefault(m.group(1), set()).add(m.group(2))
        for k in self.data["_commitments"]:
            m = self._SOID_COMM_KEY.match(k)
            if m and self.data["_commitments"][k]:
                scopes.setdefault(m.group(1), set()).add(m.group(2))
        return scopes

    # ── Commitment helpers ────────────────────────────────────────────────────

    def get_commitments(self, oid: str) -> list[dict]:
        return list(self.data["_commitments"].get(oid, []))

    def set_commitments(self, oid: str, commitments: list[dict]):
        self.data["_commitments"][oid] = commitments

    def add_commitment(self, oid: str, commitment: dict):
        """Add a commitment, deduplicating by event_id."""
        existing = self.get_commitments(oid)
        if not any(c["event_id"] == commitment["event_id"] for c in existing):
            existing.append(commitment)
            self.set_commitments(oid, existing)

    def migrate_bare_commitments(self, bare_oid: str, soid: str, calendar_id: str):
        """
        Migrate bare-oid commitments to owner-scoped key.  Called once per
        owner per unit at the start of _sync_unit.  Bare entries that can be
        claimed by this calendar (event exists here) are moved; others are
        left for the next owner to claim.
        """
        bare = self.get_commitments(bare_oid)
        if not bare or bare_oid == soid:
            return
        scoped   = self.get_commitments(soid)
        known_ids = {c["event_id"] for c in scoped}
        migrated = []
        remaining = []
        for bc in bare:
            if bc["event_id"] in known_ids:
                # Already exists in scoped — drop the bare copy
                migrated.append(bc)
            elif not bc.get("calendar_id"):
                # No calendar_id — claim it for this calendar
                bc["calendar_id"] = calendar_id
                scoped.append(bc)
                known_ids.add(bc["event_id"])
                migrated.append(bc)
            elif bc.get("calendar_id") == calendar_id:
                # Tagged for this calendar
                if bc["event_id"] not in known_ids:
                    scoped.append(bc)
                    known_ids.add(bc["event_id"])
                migrated.append(bc)
            else:
                remaining.append(bc)
        if migrated:
            self.set_commitments(soid, scoped)
            self.set_commitments(bare_oid, remaining)
            log.info(
                f"  Migrated {len(migrated)} bare-oid commitment(s) "
                f"from '{bare_oid}' → '{soid}'")

    def deduplicate_commitments(self, oid: str):
        """Remove duplicate commitments (same event_id) for a given key."""
        comms = self.get_commitments(oid)
        if len(comms) <= 1:
            return
        seen = set()
        unique = []
        for c in comms:
            eid = c.get("event_id")
            if eid and eid not in seen:
                seen.add(eid)
                unique.append(c)
        if len(unique) < len(comms):
            log.info(
                f"  Deduped commitments for {oid}: "
                f"{len(comms)} → {len(unique)}")
            self.set_commitments(oid, unique)
