"""State persistence (state.json) and the commitment registry."""
import json
from datetime import datetime
from typing import Optional

from .config import STATE_FILE, log


class StateManager:
    """
    Per occupancy+month  (existing keys unchanged):
      status, past_due,
      status_event_id, status_event_date,
      late_event_id,
      payment_event_ids,
      payment_event_dates,   — index-aligned canonical dates for
                               payment_event_ids; submit-mode payment-drag
                               detection reads them (no ledger there)
      payments,              — [{date, amount, is_nsf, description}] for ALL
                               of the month's payments (index 0 = the one
                               absorbed into the status event); NSF-reversal
                               reconciliation matches against these
      nsf_reversals_applied, — [{key, ref, date, amount}] reversals already
                               applied to this soid's month (per-calendar
                               idempotence markers; the month's own status
                               is deliberately left as written — the marker
                               and event notes are the honest annotation)
      nsf_event_ids,         — payment events flipped to NSF display after
                               their ledger row vanished (no longer
                               positionally tracked in payment_event_ids)
      last_updated

    Future-month entries additionally use:
      rent_event_id     — placeholder event ID
      is_commitment     — True when the placeholder was converted to a commitment

    Top-level commitment registry  (new in v2):
      state.data["_commitments"][oid] = [
        {
          event_id        : str,   Google Calendar event ID
          anchor_date     : str,   ISO date where PM anchored the event
          source_type     : str,   "kickstart" | "late"
          origin_month    : str,   YYYY-MM of the original event's month
          covers_rent_month: str|None  YYYY-MM if commitment crosses into a
                                       future month and pre-loads that rent
          calendar_id     : str,   Google Calendar ID this commitment lives on
        },
        ...   # one entry per split (copy-pasted events)
      ]

    Top-level calendar-id map  (cache for fast modes):
      state.data["_calendars"][owner_id] = calendar_id
      Written whenever a calendar is resolved; read by non-nightly modes to
      skip the calendarList() pagination.  The nightly full sweep re-resolves
      every calendar live and rewrites the map (heals renames/recreates).
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

    def get_calendar_id(self, owner_id) -> Optional[str]:
        return self.data["_calendars"].get(str(owner_id))

    def set_calendar_id(self, owner_id, calendar_id: str):
        self.data["_calendars"][str(owner_id)] = calendar_id

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
