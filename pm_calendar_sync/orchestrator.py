"""Sync orchestration: the per-run loop and per-unit / commitment logic."""
from datetime import date, timedelta, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from .config import (
    FORCE_REFRESH, RENT_DUE_DAY, DEFAULT_LEASE_MONTHS,
    COMMITMENT_LOOKAHEAD_MONTHS, TIMEZONE, LATE_GRACE_DAYS, log,
)
from .status import (
    classify_status, payment_status,
    STATUS_PAID, STATUS_UNPAID,
)
from .transforms import (
    normalize_tenant_name, build_owner_property_map, build_tenant_info_map,
    build_payment_map, compute_running_balances, diff_rent_roll,
    format_address, unit_label, owner_display_name, _next_day,
)
from .appfolio import AppFolioClient
from .calendar_manager import GoogleCalendarManager, _gcal_execute
from .state import StateManager
from . import cache


class SyncOrchestrator:

    def __init__(self):
        self.af    = AppFolioClient()
        self.gcal  = GoogleCalendarManager()
        self.state = StateManager()
        # Populated per run: bare occupancy_id → [(owner_id, calendar_id), ...]
        # for every owner of that unit.  Co-owned units have >1 entry; used to
        # mirror a promise created on one owner's calendar onto the co-owners'.
        self._owners_by_oid: dict = {}

    # ── Top-level run (full sweep — the sole authority for correctness) ──────

    def run(self, mode: str = "full_nightly"):
        """
        Full convergent sweep over ALL active units.

        mode="full_nightly" (default — what plain `SyncOrchestrator().run()`
        does): pulls the owner/tenant directories live (refreshing their
        cache) and refreshes calendar ACLs — identical to the historical
        behaviour, plus cache/`_calendars` writes.

        mode="full": directories come from cache/ (live-pull fallback) and
        per-owner ACL calls are skipped — the hourly backstop sweep.
        """
        log.info(f"=== OKPM sync starting (mode: {mode}) ===")
        today      = datetime.now(ZoneInfo(TIMEZONE)).date()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)
        log.info(f"  Timezone: {TIMEZONE}, local date: {today}")

        log.info("Fetching rent_roll...")
        rent_roll = self.af.get_rent_roll()
        owners, tenants = self._load_directories(mode)
        log.info("Fetching tenant_ledger (current month)...")
        ledger = self.af.get_tenant_ledger_month(
            today.replace(day=1).isoformat(), today.isoformat())

        prop_to_owner = build_owner_property_map(owners)
        tenant_info   = build_tenant_info_map(tenants)
        payment_map   = build_payment_map(ledger)
        active        = [r for r in rent_roll if r.get("status") == "Current"]
        log.info(f"  {len(active)} active leases, {len(payment_map)} with payments this month")

        # ── Diagnostic: detect non-Current leases that might be missing ───
        non_current = [r for r in rent_roll if r.get("status") != "Current"]
        if non_current:
            status_counts = {}
            for r in non_current:
                s = r.get("status", "?")
                status_counts[s] = status_counts.get(s, 0) + 1
            log.info(f"  Skipped {len(non_current)} non-Current leases: {status_counts}")

        owner_rows = self._group_rows_by_owner(active, prop_to_owner)
        owner_meta = self._resolve_owner_calendars(
            owner_rows, use_cache=(mode != "full_nightly"))
        self._build_owners_by_oid(owner_rows, owner_meta)

        for owner_id, rows_and_owners in owner_rows.items():
            owner_name, owner_email, calendar_id = owner_meta[owner_id]
            log.info(f"Owner: {owner_name} ({len(rows_and_owners)} units)")
            # ACL is refreshed nightly; other modes skip it (one acl.list per
            # owner per run) except for a calendar created this very run.
            if mode == "full_nightly" or calendar_id in self.gcal.created_calendar_ids:
                self.gcal.ensure_pm_access(calendar_id)
                if owner_email:
                    self.gcal.share_with_owner(calendar_id, owner_email)
            for row, _ in rows_and_owners:
                try:
                    self._sync_unit(
                        row, calendar_id, due_date, today, this_month,
                        tenant_info, payment_map, owner_id=owner_id,
                    )
                except Exception as exc:
                    oid = row.get("occupancy_id", "?")
                    log.error(f"  FAILED unit {oid}: {exc}", exc_info=True)

        # Snapshot is written AFTER the sweep so a crashed run never advances
        # the update-mode diff baseline past what was actually applied.  (A
        # per-unit failure caught above still advances it — the next sweep
        # retries that unit anyway.)
        self._save_rent_roll_snapshot(rent_roll)
        self.state.save()
        log.info("=== Sync complete ===")

    # ── Run-mode data plumbing ────────────────────────────────────────────────

    def _load_directories(self, mode: str) -> tuple:
        """Owner + tenant directory rows.

        full_nightly pulls live and refreshes cache/directories.json; every
        other mode reads the cache and only falls back to a live pull (also
        re-writing the cache) when it is missing or corrupt.
        """
        if mode != "full_nightly":
            cached = cache.load_json(cache.DIRECTORIES_FILE)
            if cached is not None and "owners" in cached and "tenants" in cached:
                log.info(
                    "Using cached directories "
                    f"(refreshed {cached.get('refreshed_at', '?')})")
                return cached["owners"], cached["tenants"]
        log.info("Fetching owner_directory...")
        owners = self.af.get_owner_directory()
        log.info("Fetching tenant_directory...")
        tenants = self.af.get_tenant_directory()
        cache.save_json(cache.DIRECTORIES_FILE, {
            "refreshed_at": datetime.utcnow().isoformat(),
            "owners":       owners,
            "tenants":      tenants,
        })
        return owners, tenants

    def _save_rent_roll_snapshot(self, rent_roll: list):
        """Persist the raw rent_roll rows — the diff baseline for update mode
        and the balance source for submit mode."""
        cache.save_json(cache.RENT_ROLL_FILE, {
            "refreshed_at": datetime.utcnow().isoformat(),
            "rows":         rent_roll,
        })

    def _group_rows_by_owner(self, active: list, prop_to_owner: dict) -> dict:
        """owner_id → [(rent_roll row, owner dict), ...] for every active lease."""
        owner_rows: dict = {}
        for row in active:
            pid = row.get("property_id")
            owners_list = prop_to_owner.get(pid)
            if not owners_list:
                # Try string↔int coercion in case of type mismatch
                alt_pid = int(pid) if isinstance(pid, str) and pid.isdigit() else str(pid)
                owners_list = prop_to_owner.get(alt_pid)
                if owners_list:
                    log.warning(
                        f"  property_id type mismatch for {row.get('property_name','?')}: "
                        f"rent_roll has {type(pid).__name__}({pid!r}), "
                        f"owner_map expects {type(alt_pid).__name__}")
            if owners_list:
                for own in owners_list:
                    owner_rows.setdefault(own["owner_id"], []).append((row, own))
            else:
                log.warning(
                    f"  UNMAPPED: property_id={pid!r} "
                    f"({row.get('property_name','?')}, "
                    f"tenant={row.get('tenant_name','?')}) — no owner found, skipping")
        return owner_rows

    def _resolve_owner_calendars(self, owner_rows: dict, use_cache: bool) -> dict:
        """Resolve every owner's calendar up front.

        A co-owned unit appears on multiple owner calendars.  Resolving all
        calendars before the sync loop lets each unit know its sibling
        calendars, so a promise dragged on one owner's calendar can be
        mirrored onto the co-owners' calendars in the same run.

        use_cache=True consults state's `_calendars` map first, skipping the
        calendarList() pagination for known owners; a miss falls through to a
        live resolve (creating + sharing the calendar if needed).  The nightly
        sweep passes use_cache=False — re-resolving live IS the map refresh,
        healing renames/recreates.  Returns
        owner_id → (owner_name, owner_email, calendar_id).
        """
        owner_meta: dict = {}
        for owner_id, rows_and_owners in owner_rows.items():
            owner       = rows_and_owners[0][1]
            owner_name  = owner_display_name(owner)
            owner_email = (owner.get("email") or "").strip()
            calendar_id = self.state.get_calendar_id(owner_id) if use_cache else None
            if not calendar_id:
                calendar_id = self.gcal.get_or_create_calendar(owner_name)
            self.state.set_calendar_id(owner_id, calendar_id)
            owner_meta[owner_id] = (owner_name, owner_email, calendar_id)
        return owner_meta

    def _build_owners_by_oid(self, owner_rows: dict, owner_meta: dict):
        """Populate self._owners_by_oid (bare oid → [(owner_id, calendar_id)]).
        Required by _mirror_commitment_to_siblings — every run mode that can
        create or discover commitments must call this."""
        self._owners_by_oid = {}
        for owner_id, rows_and_owners in owner_rows.items():
            calendar_id = owner_meta[owner_id][2]
            for row, _ in rows_and_owners:
                oid = str(row.get("occupancy_id"))
                self._owners_by_oid.setdefault(oid, []).append(
                    (owner_id, calendar_id))

    # ── Update mode (money-diff fast path) ────────────────────────────────────

    def run_update(self):
        """
        Manual fast path: re-sync ONLY units whose money moved since the last
        rent_roll snapshot (past_due delta > ½¢, rent change, or new lease).

        Pulls rent_roll + the current-month ledger fresh — so payment markers,
        balances and greying come out exactly as a full sweep would for those
        units — but reads directories from cache and skips ACL work.  Redraws
        driven by the calendar advancing rather than money moving (month
        rollover shifting placeholders, a due date passing) are deliberately
        NOT handled here; the hourly full sweep owns those.
        """
        log.info("=== OKPM sync starting (mode: update) ===")
        today      = datetime.now(ZoneInfo(TIMEZONE)).date()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)
        log.info(f"  Timezone: {TIMEZONE}, local date: {today}")

        owners, tenants = self._load_directories("update")
        log.info("Fetching rent_roll...")
        rent_roll = self.af.get_rent_roll()
        log.info("Fetching tenant_ledger (current month)...")
        ledger = self.af.get_tenant_ledger_month(
            today.replace(day=1).isoformat(), today.isoformat())

        snap      = cache.load_json(cache.RENT_ROLL_FILE)
        snap_rows = snap.get("rows") if snap else None
        if not isinstance(snap_rows, list):
            snap_rows = None
            log.warning("  No usable rent_roll snapshot — treating ALL units as changed")
        changed = diff_rent_roll(snap_rows, rent_roll)
        log.info(f"  {len(changed)} unit(s) with money changes since last snapshot")

        prop_to_owner = build_owner_property_map(owners)
        tenant_info   = build_tenant_info_map(tenants)
        payment_map   = build_payment_map(ledger)
        active        = [r for r in rent_roll if r.get("status") == "Current"]

        # Group + resolve for ALL active rows, not just changed ones: the
        # co-owner map must be complete for _mirror_commitment_to_siblings.
        owner_rows = self._group_rows_by_owner(active, prop_to_owner)
        owner_meta = self._resolve_owner_calendars(owner_rows, use_cache=True)
        self._build_owners_by_oid(owner_rows, owner_meta)

        synced = 0
        for owner_id, rows_and_owners in owner_rows.items():
            owner_name, owner_email, calendar_id = owner_meta[owner_id]
            # ACL only for a calendar created this very run (brand-new owner).
            if calendar_id in self.gcal.created_calendar_ids:
                self.gcal.ensure_pm_access(calendar_id)
                if owner_email:
                    self.gcal.share_with_owner(calendar_id, owner_email)
            scoped = [(row, own) for row, own in rows_and_owners
                      if str(row.get("occupancy_id")) in changed]
            if not scoped:
                continue
            log.info(f"Owner: {owner_name} ({len(scoped)} changed unit(s))")
            for row, _ in scoped:
                try:
                    self._sync_unit(
                        row, calendar_id, due_date, today, this_month,
                        tenant_info, payment_map, owner_id=owner_id,
                    )
                    synced += 1
                except Exception as exc:
                    oid = row.get("occupancy_id", "?")
                    log.error(f"  FAILED unit {oid}: {exc}", exc_info=True)

        # Even a zero-change run advances the snapshot: the baseline must
        # always reflect the last examined rent_roll.
        self._save_rent_roll_snapshot(rent_roll)
        self.state.save()
        log.info(f"=== Update complete: {synced} unit sync(s) ===")

    # ── Helpers  (unchanged) ─────────────────────────────────────────────────

    def _month_range(self, from_date: date, to_date: date) -> list[date]:
        months, cur = [], from_date.replace(day=RENT_DUE_DAY)
        end = to_date.replace(day=RENT_DUE_DAY)
        while cur <= end:
            months.append(cur)
            m = cur.month + 1
            y = cur.year + (1 if m > 12 else 0)
            m = m if m <= 12 else 1
            try:
                cur = cur.replace(year=y, month=m, day=RENT_DUE_DAY)
            except ValueError:
                import calendar as cm
                cur = cur.replace(year=y, month=m, day=cm.monthrange(y, m)[1])
        return months

    @staticmethod
    def _months_ahead(m_from: str, m_to: str) -> int:
        """Whole months from m_from to m_to (both 'YYYY-MM'); negative if earlier."""
        try:
            y1, mo1 = int(m_from[:4]), int(m_from[5:7])
            y2, mo2 = int(m_to[:4]), int(m_to[5:7])
        except (ValueError, IndexError):
            return 0
        return (y2 - y1) * 12 + (mo2 - mo1)

    def _promise_outstanding(self, anchor_month: str, unit: dict, today: date):
        """
        Combined outstanding + itemised breakdown for a promise commitment (this
        month's arrears the PM dragged into a future month).

        Stateless: everything is derived fresh from the live AppFolio `past_due`
        and `rent` each run — no stored per-tenant history — so a missed or failed
        GitHub Actions run can never make the figures drift.

        Combined  = past_due (all owed now: this month + any prior) + the rent that
                    will have accrued by the promised month.
        Breakdown = Previous balance / This month / Promised month(s).

        Returns (outstanding, breakdown) where breakdown is None unless the promise
        actually lands in a future month.
        """
        rent          = unit["rent"]
        past_due      = unit["past_due"]
        today_month   = today.strftime("%Y-%m")
        future_months = max(0, self._months_ahead(today_month, anchor_month))
        outstanding   = past_due + rent * future_months
        if future_months < 1:
            return outstanding, None

        prev_bal  = max(0.0, past_due - rent)        # arrears older than this month
        this_owed = max(0.0, past_due - prev_bal)    # = min(past_due, rent), clamped
        this_lbl  = today.strftime("%b %Y")
        try:
            prom_lbl = date.fromisoformat(anchor_month + "-01").strftime("%b %Y")
        except ValueError:
            prom_lbl = anchor_month
        prom_label = prom_lbl if future_months == 1 else f"{future_months} mo → {prom_lbl}"
        breakdown = [
            ("Previous balance",        prev_bal),
            (f"This month ({this_lbl})", this_owed),
            (f"Promised ({prom_label})", rent * future_months),
        ]
        return outstanding, breakdown

    def _mirror_commitment_to_siblings(
        self, oid: str, commitment: dict, unit: dict, today: date,
    ):
        """
        Mirror a promise onto co-owners' calendars.

        A co-owned unit (owned by multiple owners / portfolios) shows up on one
        calendar per owner.  When the PM drags an event on ONE owner's calendar,
        the promise is created only there; without this, the co-owners never see
        it.  For every OTHER owner of the same occupancy, create an equivalent
        commitment event on their calendar and register it in their commitment
        state.  Each co-owner's own `_sync_unit` then suppresses that month's
        status/placeholder normally (this run if not yet processed, next run
        otherwise) and thereafter treats the promise as its own.

        Idempotent: a sibling that already carries a commitment with the same
        source_type + anchor_date is skipped, so repeated runs, the sibling's own
        rediscovery, and split copies never spawn duplicates.
        """
        siblings = self._owners_by_oid.get(oid, [])
        if len(siblings) < 2:
            return

        origin_cal   = commitment.get("calendar_id")
        anchor       = commitment.get("anchor_date") or today.isoformat()
        source       = commitment.get("source_type") or "late"
        origin_month = commitment.get("origin_month") or anchor[:7]
        covers       = commitment.get("covers_rent_month")
        today_month  = today.strftime("%Y-%m")

        def _is_promise(src: str) -> bool:
            return src in ("status", "payment", "late")

        for owner_id, cal_id in siblings:
            if cal_id == origin_cal:
                continue
            sib_soid = f"{oid}@{owner_id}"
            self.state.migrate_bare_commitments(oid, sib_soid, cal_id)
            self.state.deduplicate_commitments(sib_soid)
            existing = self.state.get_commitments(sib_soid)
            if any(c.get("calendar_id") == cal_id
                   and (c.get("source_type") or "late") == source
                   and (c.get("anchor_date") or "") == anchor
                   for c in existing):
                continue

            # Mirror the origin's outstanding / breakdown computation.
            breakdown = None
            if _is_promise(source):
                outstanding, breakdown = self._promise_outstanding(
                    anchor[:7], unit, today)
                disp = classify_status(unit["rent"], unit["past_due"])
            else:
                outstanding = unit["past_due"] + (
                    unit["rent"] if (covers and covers > today_month) else 0.0)
                disp = ""

            body = self.gcal._build_commitment_event(
                unit, anchor, source, max(0.0, outstanding),
                source_status=disp, breakdown=breakdown)
            try:
                created = _gcal_execute(self.gcal.service.events().insert(
                    calendarId=cal_id, body=body))
            except HttpError as e:
                log.error(
                    f"  {oid}: failed to mirror {source} promise to "
                    f"co-owner calendar {cal_id}: {e}")
                continue

            self.state.add_commitment(sib_soid, {
                "event_id":          created["id"],
                "anchor_date":       anchor,
                "source_type":       source,
                "origin_month":      origin_month,
                "calendar_id":       cal_id,
                "covers_rent_month": covers,
            })
            log.info(
                f"  {oid}: mirrored {source} promise on {anchor} to co-owner "
                f"calendar {cal_id}")

    def _make_unit(self, row: dict, tenant_info: dict, payment_map: dict) -> dict:
        oid      = str(row["occupancy_id"])
        rent     = float(row.get("rent", 0) or 0)
        past_due = float(row.get("past_due", 0) or 0)
        info     = tenant_info.get(int(oid), {})
        t_norm   = normalize_tenant_name(row.get("tenant", ""))

        # ── Match payments by primary tenant AND additional tenants ────────
        # The AppFolio tenant_ledger has no occupancy_id field, so name-
        # matching is the only option.  Check primary first, then fall back
        # to additional tenants (deduplicated).
        payments = payment_map.get(t_norm, [])
        if not payments:
            addl = row.get("additional_tenants", "")
            if addl:
                for name in addl.split(","):
                    name_norm = normalize_tenant_name(name.strip())
                    if name_norm and name_norm in payment_map:
                        payments = payment_map[name_norm]
                        break

        amount_paid = sum(p["amount"] for p in payments if not p["is_nsf"])
        return {
            "occupancy_id":       oid,
            "property_name":      row.get("property_name", ""),
            "address":            format_address(row),
            "unit_label":         unit_label(row),
            "tenant":             row.get("tenant", ""),
            "additional_tenants": row.get("additional_tenants", ""),
            "rent":               rent,
            "past_due":           past_due,
            "amount_paid":        amount_paid,
            "payments":           payments,
            "phone":              info.get("phone", "N/A"),
            "late_fee_desc":      info.get("late_fee_desc", "N/A"),
            "grace_days":         info.get("grace_days", LATE_GRACE_DAYS),
            "lease_from":         row.get("lease_from", ""),
            "lease_to":           row.get("lease_to", "") or "",
        }

    # ── Drag detection (shared by the full sweep and submit mode) ────────────
    # Each primitive takes an optional events_by_id index (event_id → event
    # body from ONE unbounded listing of the calendar).  With no index (full
    # sweep) it issues the same live GETs as always — byte-identical traffic.
    # With an index (submit mode) a missing id means the event is GONE; that
    # equivalence holds ONLY because submit's listing has no time window.

    def _live_event_start(self, calendar_id: str, event_id: str,
                          events_by_id: Optional[dict] = None) -> Optional[str]:
        """Live ISO start date of an event; None/'' when it is gone.  Mirrors
        gcal.get_event_start_date semantics exactly when reading the index."""
        if events_by_id is not None:
            ev = events_by_id.get(event_id)
            if not ev:
                return None
            start = ev.get("start", {})
            return start.get("date") or start.get("dateTime", "")[:10]
        return self.gcal.get_event_start_date(calendar_id, event_id)

    def _convert_status_drag(
        self, soid: str, calendar_id: str, unit: dict,
        status_event_id: str, canonical_date: str,
        today: date, this_month: str, commitment_months: set,
        source_status: str,
        events_by_id: Optional[dict] = None,
    ) -> bool:
        """Drag site 1: the current-month STATUS event dragged to a future
        date → convert it in place to a commitment (retire & replace),
        register + mirror the promise, and mark this_month covered.  Returns
        True on conversion; the caller owns its flag/state updates."""
        live = self._live_event_start(calendar_id, status_event_id, events_by_id)
        if not (live and live != canonical_date and live > today.isoformat()):
            return False
        oid      = soid.split("@")[0] if "@" in soid else soid
        past_due = unit["past_due"]
        self.gcal.convert_to_commitment(
            calendar_id, status_event_id, unit, live, "status",
            max(0.0, past_due), source_status=source_status)
        _commit = {
            "event_id":          status_event_id,
            "anchor_date":       live,
            "source_type":       "status",
            "origin_month":      this_month,
            "calendar_id":       calendar_id,
            "covers_rent_month": this_month,
        }
        self.state.add_commitment(soid, _commit)
        self._mirror_commitment_to_siblings(oid, _commit, unit, today)
        commitment_months.add(this_month)
        log.info(
            f"  {oid}: status event dragged to {live} → "
            f"converted in place to commitment (month suppressed)")
        return True

    def _detect_status_snapback(
        self, soid: str, calendar_id: str, status_event_id: str,
        canonical_date: str, unit: dict, today: date, this_month: str,
        past_due: float, status: str, commitment_months: set,
        events_by_id: Optional[dict] = None,
    ):
        """Locked STATUS event: a forward drag (with no commitment covering
        this month yet) spawns a NEW commitment at the target, then the
        original snaps back to its canonical date; a backward drag reverts."""
        live = self._live_event_start(calendar_id, status_event_id, events_by_id)
        canon = canonical_date
        if not (live and live != canon):
            return
        if live > today.isoformat():
            if this_month not in commitment_months:
                new_body = self.gcal._build_commitment_event(
                    unit, live, "status", max(0.0, past_due),
                    source_status=status)
                created = _gcal_execute(self.gcal.service.events().insert(
                    calendarId=calendar_id, body=new_body))
                _commit = {
                    "event_id":          created["id"],
                    "anchor_date":       live,
                    "source_type":       "status",
                    "origin_month":      this_month,
                    "calendar_id":       calendar_id,
                    "covers_rent_month": this_month,
                }
                self.state.add_commitment(soid, _commit)
                self._mirror_commitment_to_siblings(
                    soid.split("@")[0], _commit, unit, today)
                commitment_months.add(this_month)
                log.info(
                    f"  {soid}: status event dragged to {live} "
                    f"→ commitment registered")
            if events_by_id is not None:
                ev = events_by_id.get(status_event_id)
                ev = dict(ev) if ev else None
            else:
                ev = self.gcal.get_event(calendar_id, status_event_id)
            if ev:
                ev["start"] = {"date": canon}
                ev["end"]   = {"date": _next_day(canon)}
                try:
                    _gcal_execute(self.gcal.service.events().update(
                        calendarId=calendar_id,
                        eventId=status_event_id, body=ev))
                except HttpError as e:
                    log.error(f"  Failed to snap back {status_event_id}: {e}")
        else:
            self.gcal.revert_event_to_date(
                calendar_id, status_event_id, canon)

    def _detect_payment_drags(
        self, soid: str, calendar_id: str,
        payment_event_ids: list, payment_dates: list,
        unit: dict, today: date, this_month: str,
        past_due: float, status: str, commitment_months: set,
        events_by_id: Optional[dict] = None,
    ):
        """Locked PAYMENT events (idx 1+): a forward drag spawns a NEW
        commitment at the target (unless one already covers this month), then
        the marker is snapped back — a received payment's date can never move.
        payment_dates is index-aligned with payment_event_ids."""
        for i, event_id in enumerate(payment_event_ids):
            if i < len(payment_dates):
                pay_canon = payment_dates[i]
                live = self._live_event_start(calendar_id, event_id, events_by_id)
                if live and live != pay_canon:
                    if (live > today.isoformat()
                            and this_month not in commitment_months):
                        new_body = self.gcal._build_commitment_event(
                            unit, live, "payment", max(0.0, past_due),
                            source_status=status)
                        created = _gcal_execute(
                            self.gcal.service.events().insert(
                                calendarId=calendar_id, body=new_body))
                        _commit = {
                            "event_id":          created["id"],
                            "anchor_date":       live,
                            "source_type":       "payment",
                            "origin_month":      this_month,
                            "calendar_id":       calendar_id,
                            "covers_rent_month": this_month,
                        }
                        self.state.add_commitment(soid, _commit)
                        self._mirror_commitment_to_siblings(
                            soid.split("@")[0], _commit, unit, today)
                        commitment_months.add(this_month)
                        log.info(
                            f"  {soid}: payment event dragged to {live} "
                            f"→ commitment registered")
                    self.gcal.revert_event_to_date(
                        calendar_id, event_id, pay_canon)

    def _convert_kickstart_drag(
        self, soid: str, calendar_id: str, unit: dict,
        fmonth: str, expected_iso: str, prior_f: dict, today: date,
        events_by_id: Optional[dict] = None,
    ) -> str:
        """Drag site 3: a future-month KICKSTART placeholder that was dragged
        → convert it in place to a kickstart commitment.  Returns
        'converted' | 'ignored_past' | 'missing' | 'unmoved' so the caller
        keeps its exact loop flow (continue / clear id / fall through)."""
        oid            = soid.split("@")[0] if "@" in soid else soid
        placeholder_id = prior_f.get("rent_event_id")
        past_due       = unit["past_due"]
        live_date = self._live_event_start(
            calendar_id, placeholder_id, events_by_id)
        if live_date and live_date != expected_iso:
            if live_date > today.isoformat():
                self.gcal.convert_to_commitment(
                    calendar_id, placeholder_id, unit,
                    live_date, "kickstart", max(0.0, past_due),
                    source_status=STATUS_UNPAID,
                )
                _commit = {
                    "event_id":           placeholder_id,
                    "anchor_date":        live_date,
                    "source_type":        "kickstart",
                    "origin_month":       fmonth,
                    "calendar_id":        calendar_id,
                    "covers_rent_month":  fmonth,
                }
                self.state.add_commitment(soid, _commit)
                self._mirror_commitment_to_siblings(oid, _commit, unit, today)
                self.state.set(soid, fmonth, {
                    **prior_f, "is_commitment": True,
                })
                log.info(
                    f"  {oid}: kickstart for {fmonth} moved "
                    f"to {live_date} → commitment registered")
                return "converted"
            log.warning(
                f"  {oid}: kickstart for {fmonth} moved to "
                f"{live_date} (past/today) — ignoring, not a future commitment")
            return "ignored_past"
        if live_date is None:
            log.warning(
                f"  {oid}: kickstart {placeholder_id} for {fmonth} — "
                f"event not found in Google (deleted?)")
            self.state.set(soid, fmonth, {**prior_f, "rent_event_id": None})
            return "missing"
        return "unmoved"

    # ── Per-unit sync  (core v2 logic) ───────────────────────────────────────

    def _sync_unit(
        self, row: dict, calendar_id: str, due_date: date,
        today: date, this_month: str, tenant_info: dict, payment_map: dict,
        owner_id: str = "",
    ):
        unit     = self._make_unit(row, tenant_info, payment_map)
        oid      = unit["occupancy_id"]   # bare — for Google Calendar
        soid     = f"{oid}@{owner_id}" if owner_id else oid  # for state keys
        rent     = unit["rent"]
        past_due = unit["past_due"]
        status   = classify_status(rent, past_due)
        # When the month is settled (balance 0) or in credit (< 0), earlier and
        # failed payment events are recoloured grey to cut visual noise; the
        # settling (green) and prepaid (pink) events stay coloured.
        month_fully_paid = past_due <= 0

        try:
            lease_end = date.fromisoformat(unit["lease_to"])
        except ValueError:
            m = due_date.month + DEFAULT_LEASE_MONTHS
            y = due_date.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            lease_end = date(y, m, 1)

        sorted_payments = sorted(unit["payments"], key=lambda p: (p["date"], -p["amount"]))

        # ── Filter payments to current month ───────────────────────────────
        sorted_payments = [
            p for p in sorted_payments
            if p["date"][:7] >= this_month
        ]
        # Update amount_paid to reflect filtered payments only
        unit["amount_paid"] = sum(
            p["amount"] for p in sorted_payments if not p["is_nsf"])

        balances        = compute_running_balances(sorted_payments, past_due)
        prior           = self.state.get(soid, this_month)

        # Distrust state written for a DIFFERENT calendar.
        if prior and prior.get("calendar_id") != calendar_id:
            prior = None

        # ── Load commitment state ─────────────────────────────────────────────
        # Migrate any bare-oid commitment entries to this owner-scoped key,
        # then deduplicate.  This fixes the runaway duplication bug where bare
        # entries without calendar_id were processed for every calendar.
        self.state.migrate_bare_commitments(oid, soid, calendar_id)
        self.state.deduplicate_commitments(soid)

        commitments = self.state.get_commitments(soid)
        # Months covered by any commitment (for kickstart suppression)
        commitment_months = {
            c["covers_rent_month"] for c in commitments
            if c.get("covers_rent_month")
        }

        # ── Status event date ─────────────────────────────────────────────────
        if sorted_payments:
            status_event_date = date.fromisoformat(sorted_payments[0]["date"])
            # Safety clamp: never place the event before this month's due date
            status_event_date = max(status_event_date, due_date)
            first_pay         = sorted_payments[0]
            # A payment was received this month → use payment_status so the event
            # reads 🟡 Partial (never 🔴) as long as any balance remains, even
            # when the tenant is one+ months in arrears.
            event_status      = payment_status(rent, balances[0])
        else:
            status_event_date = due_date
            first_pay         = None
            event_status      = status

        # ── Kickstart suppression ─────────────────────────────────────────────
        # When a commitment covers this month and no payments exist yet,
        # we skip creating/keeping a status event on the 1st.
        suppress_kickstart = (
            this_month in commitment_months and not sorted_payments
        )

        if suppress_kickstart:
            # If transitioning from a placeholder that is NOT itself the
            # commitment event, delete it so only the commitment shows.
            if (prior
                    and prior.get("rent_event_id")
                    and not prior.get("status_event_id")
                    and not prior.get("is_commitment")):
                log.info(
                    f"  {oid}: commitment covers {this_month} — removing stale placeholder")
                self.gcal.delete_event(calendar_id, prior["rent_event_id"])
                prior = {**prior, "rent_event_id": None}
            # Clean up any status/rent events left over for a commitment-covered
            # month.  Never delete the commitment event itself.
            commit_ids = {c.get("event_id") for c in commitments}
            for _etype in ("status", "rent"):
                for _ in range(4):  # safety bound
                    sid = self.gcal._find_event(calendar_id, oid, this_month, _etype)
                    if not sid or sid in commit_ids:
                        break
                    self.gcal.delete_event(calendar_id, sid)
                    log.info(
                        f"  {oid}: removed stale {_etype} event "
                        f"(commitment covers {this_month})")
            if prior and prior.get("status_event_id") not in commit_ids:
                prior = {**prior, "status_event_id": None}

        # ── prior_status_id resolution ────────────────────────────────────────
        prior_status_id   = (prior.get("status_event_id") or
                             prior.get("rent_event_id")) if prior else None
        prior_status_date = (
            prior.get("status_event_date", due_date.isoformat()) if prior
            else due_date.isoformat()
        )
        date_changed  = prior_status_date != status_event_date.isoformat()
        data_changed  = not (
            prior
            and prior["status"] == status
            and prior["past_due"] == past_due
        )
        new_payments  = (
            len(sorted_payments) > (
                len(prior.get("payment_event_ids", [])) +
                (1 if (prior_status_id
                       and prior_status_date != due_date.isoformat()) else 0)
            )
            if prior else bool(sorted_payments)
        )

        # ── Detect dragged status event → commitment (retire & replace) ───────
        if (prior and prior.get("status_event_id")
                and not FORCE_REFRESH
                and not suppress_kickstart
                and this_month not in commitment_months):
            if self._convert_status_drag(
                    soid, calendar_id, unit,
                    prior["status_event_id"],
                    prior.get("status_event_date", due_date.isoformat()),
                    today, this_month, commitment_months,
                    source_status=status):
                suppress_kickstart = True
                prior = {**prior, "status_event_id": None}

        # ── Build / update status event ───────────────────────────────────────
        if suppress_kickstart:
            # Commitment anchors this month; no status event on the 1st
            status_event_id = None
            log.info(f"  {oid}: status event suppressed (commitment anchors {this_month})")
        elif FORCE_REFRESH or date_changed or data_changed:
            body = self.gcal._build_status_event(
                unit, event_status, status_event_date,
                first_pay,
                balances[0] if balances else None,
                total_payments=len(sorted_payments),
                month_fully_paid=month_fully_paid,
            )
            existing_id = (
                prior_status_id or
                self.gcal._find_status_event(calendar_id, oid, this_month)
            )
            status_event_id = self.gcal._update_or_create(
                calendar_id, existing_id, body)
            log.info(f"  Status event {oid}: {event_status} on {status_event_date}")
        elif prior_status_id is None:
            # RECOVERY: a prior run suppressed this event (commitment was active)
            # but the commitment has since been removed or resolved.  The state
            # has status_event_id=None and rent_event_id=None, data hasn't changed,
            # so the normal paths all skip.  Force-create the missing event.
            body = self.gcal._build_status_event(
                unit, event_status, status_event_date,
                first_pay,
                balances[0] if balances else None,
                total_payments=len(sorted_payments),
                month_fully_paid=month_fully_paid,
            )
            # Search the calendar first to avoid creating duplicates
            existing_id = self.gcal._find_status_event(calendar_id, oid, this_month)
            status_event_id = self.gcal._update_or_create(
                calendar_id, existing_id, body)
            log.warning(
                f"  {oid}: status event was missing (post-suppression recovery) "
                f"— {'updated' if existing_id else 'created'} on {status_event_date}")
        else:
            status_event_id = prior_status_id
            # SELF-HEAL: verify the event still exists on Google Calendar.
            # A previous bug could have deleted events while state retained
            # their stale IDs.
            if not self.gcal.get_event(calendar_id, status_event_id):
                log.warning(
                    f"  {oid}: status event {status_event_id} orphaned "
                    f"(missing from calendar) — recreating")
                body = self.gcal._build_status_event(
                    unit, event_status, status_event_date,
                    first_pay,
                    balances[0] if balances else None,
                    total_payments=len(sorted_payments),
                )
                # Search the calendar first to avoid creating duplicates
                existing_id = self.gcal._find_status_event(
                    calendar_id, oid, this_month)
                status_event_id = self.gcal._update_or_create(
                    calendar_id, existing_id, body)
            else:
                log.info(f"  No change for {oid} — skipping status event")

        # ── Additional payment events ─────────────────────────────────────────
        if FORCE_REFRESH or data_changed or new_payments:
            payment_event_ids = self._sync_additional_payments(
                unit, calendar_id, this_month,
                sorted_payments[1:], balances[1:], prior,
                month_fully_paid=month_fully_paid,
            )
        else:
            payment_event_ids = prior.get("payment_event_ids", []) if prior else []

        # ── Detect-and-revert locked events ──────────────────────────────────
        if not suppress_kickstart and not (FORCE_REFRESH or date_changed or data_changed):
            self._verify_locked_events(
                soid, calendar_id, prior,
                status_event_id, status_event_date, sorted_payments,
                unit, today, this_month, past_due, status, commitment_months,
            )

        # ── Retire any legacy "today" / late preview event ────────────────────
        prior_late_id = prior.get("late_event_id") if prior else None
        if prior_late_id:
            self.gcal.delete_event(calendar_id, prior_late_id)
            log.info(f"  {oid}: removed retired today/late preview event")
        late_event_id = None

        # ── Process all commitments for this unit ─────────────────────────────
        # ALWAYS scan the calendar for commitment events, even when state has no
        # record of any.  Dragged promises live as commitment events on the
        # calendar (okpm_event_type="commitment") — that is the source of truth.
        # Scanning unconditionally makes the system self-recovering: after a
        # state wipe or corruption repair, promises are rediscovered from the
        # calendar and re-tracked automatically.  (A unit with no commitment
        # events costs one empty list call and returns immediately.)
        self._process_commitments(
            soid, calendar_id, unit, today,
            has_known_or_new=True,
        )

        # ── Persist current-month state ───────────────────────────────────────
        self.state.set(soid, this_month, {
            "status":            status,
            "past_due":          past_due,
            "calendar_id":       calendar_id,
            "status_event_id":   status_event_id,
            "status_event_date": status_event_date.isoformat(),
            "late_event_id":     late_event_id,
            "payment_event_ids": payment_event_ids,
            # Index-aligned with payment_event_ids.  Submit mode has no ledger,
            # so payment-drag detection reads canonical dates from here.
            "payment_event_dates": [p["date"] for p in sorted_payments[1:]],
        })

        # ── B. Future months ──────────────────────────────────────────────────
        future_unit = {**unit, "past_due": 0.0, "amount_paid": 0.0, "payments": []}
        has_credit  = past_due < 0

        if FORCE_REFRESH:
            all_unit_evs, _pt = [], None
            while True:
                _resp = _gcal_execute(self.gcal.service.events().list(
                    calendarId=calendar_id,
                    privateExtendedProperty=[f"okpm_occupancy_id={oid}"],
                    showDeleted=False, maxResults=250, pageToken=_pt,
                ))
                all_unit_evs.extend(_resp.get("items", []))
                _pt = _resp.get("nextPageToken")
                if not _pt:
                    break
            purged = 0
            for ev in all_unit_evs:
                props = ev.get("extendedProperties", {}).get("private", {})
                ev_month = props.get("okpm_month", "")
                ev_type  = props.get("okpm_event_type", "")
                if ev_month > this_month and ev_type != "commitment":
                    self.gcal.delete_event(calendar_id, ev["id"])
                    purged += 1
            if purged:
                log.info(f"  {oid}: purged {purged} future-month event(s) before rebuild")

        # Reload commitments (may have new additions from the late-event detection)
        commitments = self.state.get_commitments(soid)

        for i, fdue in enumerate(
            self._month_range(due_date + timedelta(days=32), lease_end)
        ):
            fmonth  = fdue.strftime("%Y-%m")
            prior_f = self.state.get(soid, fmonth)
            if prior_f and prior_f.get("calendar_id") != calendar_id:
                prior_f = None
            is_next = (i == 0)

            # ── Check if a commitment already covers this month ───────────────
            commitment_covers_month = any(
                (c.get("source_type") == "kickstart"
                 and c.get("origin_month") == fmonth) or
                c.get("covers_rent_month") == fmonth
                for c in commitments
            )
            if commitment_covers_month:
                continue

            # ── A promise dragged into this month absorbs its placeholder ─────
            # A status/payment/late commitment landing in (or beyond) this month
            # already folds this month's rent into its combined total, so a
            # separate placeholder here would double-count.  Delete any existing
            # placeholder and skip creation while the promise stands; if the
            # promise later resolves or is dragged back, the placeholder returns.
            promise_absorbs_month = any(
                c.get("source_type") in ("status", "payment", "late")
                and (c.get("anchor_date") or "")[:7] >= fmonth
                for c in commitments
            )
            if promise_absorbs_month:
                if (prior_f and prior_f.get("rent_event_id")
                        and not prior_f.get("is_commitment")):
                    self.gcal.delete_event(calendar_id, prior_f["rent_event_id"])
                    self.state.set(soid, fmonth,
                                   {**prior_f, "rent_event_id": None})
                    log.info(
                        f"  {oid}: promise absorbs {fmonth} placeholder — removed")
                continue

            # ── Scan first COMMITMENT_LOOKAHEAD_MONTHS for moved kickstarts ──
            if prior_f and i < COMMITMENT_LOOKAHEAD_MONTHS and not FORCE_REFRESH:
                placeholder_id = prior_f.get("rent_event_id")
                if placeholder_id and not prior_f.get("is_commitment"):
                    outcome = self._convert_kickstart_drag(
                        soid, calendar_id, unit, fmonth, fdue.isoformat(),
                        prior_f, today)
                    if outcome == "converted":
                        continue
                    if outcome == "missing":
                        prior_f = {**prior_f, "rent_event_id": None}

            # ── Normal frozen-placeholder logic ─────────────────────────────────
            if not FORCE_REFRESH and prior_f and prior_f.get("rent_event_id") and not (is_next and has_credit):
                continue

            if is_next and has_credit:
                projected  = rent + past_due
                if projected <= 0:
                    fut_status = STATUS_PAID
                else:
                    fut_status = classify_status(rent, projected)
                this_fu    = {
                    **future_unit,
                    "past_due":    max(0.0, projected),
                    "amount_paid": abs(past_due),
                }
                log.info(
                    f"  Next month {oid}: credit=${abs(past_due):,.2f} "
                    f"→ balance=${max(0, projected):,.2f}")
            else:
                fut_status = STATUS_UNPAID
                this_fu    = future_unit

            body = self.gcal._build_future_placeholder(this_fu, fut_status, fdue)
            eid  = self.gcal.upsert_event(calendar_id, body)
            self.state.set(soid, fmonth, {
                "status":        fut_status,
                "past_due":      this_fu["past_due"],
                "calendar_id":   calendar_id,
                "rent_event_id": eid,
                "late_event_id": None,
            })

    # ── Additional payments  (unchanged) ─────────────────────────────────────

    def _sync_additional_payments(
        self, unit: dict, calendar_id: str, this_month: str,
        additional: list[dict], balances: list[float], prior: Optional[dict],
        month_fully_paid: bool = False,
    ) -> list[str]:
        """Sync payment events for idx 1+. Payment 0 is absorbed into status event."""
        prior_ids  = prior.get("payment_event_ids", []) if prior else []
        month_recv = unit["amount_paid"]
        total      = len(additional) + 1
        event_ids  = []

        for i, (payment, balance) in enumerate(zip(additional, balances)):
            body = self.gcal._build_additional_payment_event(
                unit, payment, i + 2, total, balance, month_recv,
                month_fully_paid=month_fully_paid)
            existing = prior_ids[i] if i < len(prior_ids) else None
            if not existing:
                existing = self.gcal._find_payment_event(
                    calendar_id, unit["occupancy_id"], this_month, i + 1)
            eid = self.gcal._update_or_create(calendar_id, existing, body)
            event_ids.append(eid)
            log.info(
                f"  Payment {i+2}/{total} for {unit['occupancy_id']} "
                f"on {payment['date']}")

        return event_ids

    # ── Locked-event revert ───────────────────────────────────────────────────

    def _verify_locked_events(
        self,
        soid: str,
        calendar_id: str,
        prior: Optional[dict],
        status_event_id: Optional[str],
        canonical_status_date: date,
        sorted_payments: list[dict],
        unit: dict,
        today: date,
        this_month: str,
        past_due: float,
        status: str,
        commitment_months: set,
    ):
        """
        Read the live date of each locked event (status + payment logs) from
        Google.  If dragged to a future date and no commitment already covers
        this month, create a commitment at the target date and then snap the
        event back.  Otherwise just revert.  (Thin composition of the shared
        drag-detection primitives above.)
        """
        if not prior or not status_event_id:
            return

        # ── Status event ──────────────────────────────────────────────────
        if prior.get("status_event_id"):
            self._detect_status_snapback(
                soid, calendar_id, status_event_id,
                canonical_status_date.isoformat(), unit, today, this_month,
                past_due, status, commitment_months)

        # ── Payment events ────────────────────────────────────────────────
        self._detect_payment_drags(
            soid, calendar_id,
            prior.get("payment_event_ids", []),
            [p["date"] for p in sorted_payments[1:]],
            unit, today, this_month, past_due, status, commitment_months)

    # ── Commitment lifecycle ──────────────────────────────────────────────────

    def _process_commitments(
        self,
        soid: str,
        calendar_id: str,
        unit: dict,
        today: date,
        has_known_or_new: bool = False,
        events: Optional[list] = None,
    ):
        """
        For each tracked commitment:
          1. Discover new copies (PM copy-pasted for split payment plans).
          2. Resolve (delete every promise) if account balance ≤ 0.
          3. Update the auto section, preserving PM notes above the divider.
             Display recomputes from the live balance: 🔴 when nothing has been
             paid this month, 🟡 when a partial payment leaves a balance.  A
             promise whose date has already passed is NOT specially flagged — it
             keeps its 🔴 / 🟡 colour (no auto-expire, no ⚠️ overdue state).
             Also picks up re-drags (PM moved the commitment again).
          4. Safe delete (≥1-promise rule): a deleted promise sticks only while
             another promise remains; deleting the LAST promise recreates one.
          Kickstart placeholders keep their own recreate + drag-back-to-1st
          behaviour and are exempt from the ≥1-promise rule.

        Optimisation: skips the Google list call entirely when no commitments
        are known and none were registered this run.

        events, when provided (submit mode), must be exactly this unit's
        commitment-typed events from an UNBOUNDED listing of the calendar.
        A time-windowed listing would misreport out-of-window promises as
        PM-deleted and trip the ≥1-promise recreation — never pass a
        windowed subset.
        """
        if not has_known_or_new:
            return

        bare_oid = soid.split("@")[0] if "@" in soid else soid
        if events is not None:
            live_events = events
        else:
            live_events = self.gcal.find_all_events_by_type(
                calendar_id, bare_oid, "commitment")
        live_by_id  = {ev["id"]: ev for ev in live_events}

        commitments = self.state.get_commitments(soid)

        # Discover split copies that PM created (copy-paste in Google Calendar)
        known_ids = {c["event_id"] for c in commitments}
        for ev in live_events:
            if ev["id"] not in known_ids:
                anchor = ev.get("start", {}).get("date", today.isoformat())
                src    = (ev.get("extendedProperties", {})
                          .get("private", {})
                          .get("okpm_source_type", "late"))
                today_month = today.strftime("%Y-%m")
                new_c = {
                    "event_id":           ev["id"],
                    "anchor_date":        anchor,
                    "source_type":        src,
                    "origin_month":       anchor[:7],
                    "calendar_id":        calendar_id,
                    "covers_rent_month":  (
                        anchor[:7] if (src == "late" and anchor[:7] > today_month)
                        else anchor[:7] if src == "kickstart"
                        else today_month if src in ("status", "payment")
                        else None
                    ),
                }
                self.state.add_commitment(soid, new_c)
                self._mirror_commitment_to_siblings(bare_oid, new_c, unit, today)
                log.info(f"  {bare_oid}: discovered new split commitment on {anchor}")

        # Reload after potential additions
        commitments = self.state.get_commitments(soid)
        if not commitments:
            return

        past_due    = unit["past_due"]
        rent        = unit["rent"]
        today_str   = today.isoformat()
        today_month = today.strftime("%Y-%m")
        surviving        = []
        missing_promises = []

        def _is_promise(src: str) -> bool:
            return src in ("status", "payment", "late")

        for c in commitments:
            # Skip commitments that belong to a different calendar.
            c_cal = c.get("calendar_id")
            if c_cal and c_cal != calendar_id:
                surviving.append(c)
                continue

            event_id          = c["event_id"]
            anchor_date       = c.get("anchor_date") or today_str
            source_type       = c.get("source_type") or "late"
            covers_rent_month = c.get("covers_rent_month")

            # ── Resolve if fully paid ─────────────────────────────────────────
            if past_due <= 0:
                if event_id in live_by_id:
                    self.gcal.delete_event(calendar_id, event_id)
                    log.info(
                        f"  {bare_oid}: commitment {event_id} resolved "
                        f"(balance ≤ 0), deleted")
                continue

            # ── PM deleted the event ──────────────────────────────────────────
            ev_body = live_by_id.get(event_id)
            if ev_body is None:
                if _is_promise(source_type):
                    missing_promises.append(c)
                    continue
                # Kickstart placeholder: recreate
                outstanding = past_due + (
                    rent if (covers_rent_month and covers_rent_month > today_month)
                    else 0
                )
                new_body = self.gcal._build_commitment_event(
                    unit, anchor_date, source_type, outstanding)
                try:
                    created = _gcal_execute(self.gcal.service.events().insert(
                        calendarId=calendar_id, body=new_body))
                    c = {**c, "event_id": created["id"], "calendar_id": calendar_id}
                    ev_body = new_body
                    live_by_id[c["event_id"]] = new_body
                    log.info(
                        f"  {bare_oid}: recreated deleted kickstart on {anchor_date}")
                except HttpError as e:
                    log.error(f"  {bare_oid}: failed to recreate kickstart: {e}")
                    continue

            # ── Kickstart drag-back to origin 1st → revert to placeholder ──
            live_date = ev_body.get("start", {}).get("date", anchor_date)
            if source_type == "kickstart":
                origin_first = f"{c.get('origin_month', anchor_date[:7])}-01"
                if live_date == origin_first:
                    log.info(
                        f"  {bare_oid}: kickstart {c['event_id']} dragged back to "
                        f"{live_date} — reverting to placeholder")
                    self.gcal.delete_event(calendar_id, c["event_id"])
                    prior_f = self.state.get(soid, c.get("origin_month", ""))
                    if prior_f:
                        self.state.set(soid, c.get("origin_month", ""), {
                            **prior_f,
                            "rent_event_id": None,
                            "is_commitment": False,
                        })
                    continue

            # ── Compute displayed outstanding (+ breakdown for promises) ──────
            # A promise dragged into a future month shows a COMBINED total with an
            # itemised breakdown (previous / this month / promised); a kickstart or
            # in-month promise keeps the single-line display.
            breakdown = None
            if _is_promise(source_type):
                outstanding, breakdown = self._promise_outstanding(
                    anchor_date[:7], unit, today)
            elif covers_rent_month and covers_rent_month > today_month:
                outstanding = past_due + rent
            else:
                outstanding = past_due

            # ── Display status: 🔴 if nothing paid this month, 🟡 if a partial
            #    payment leaves a balance.  A promise whose date has already
            #    passed is NOT specially flagged — it keeps its 🔴 / 🟡 balance
            #    colour (the old ⚠️ Overdue / tangerine state was removed).
            display_status = ""
            if _is_promise(source_type):
                display_status = classify_status(rent, past_due)

            # ── Update event (preserves PM notes, picks up re-drags) ──────────
            event_id = c["event_id"]
            new_live_anchor = self.gcal.update_commitment_event(
                calendar_id, event_id, ev_body,
                unit, anchor_date, source_type, outstanding,
                source_status=display_status, breakdown=breakdown,
            )

            # Persist any changes
            updated_c = {**c, "anchor_date": new_live_anchor, "calendar_id": calendar_id}
            if source_type == "late":
                updated_c["covers_rent_month"] = (
                    new_live_anchor[:7]
                    if new_live_anchor[:7] > today_month
                    else None
                )
            elif source_type in ("status", "payment"):
                updated_c["covers_rent_month"] = (
                    c.get("covers_rent_month")
                    or c.get("origin_month")
                    or today_month
                )

            surviving.append(updated_c)

        # ── ≥1-promise rule ─────────────────────────────────────────────────
        if (past_due > 0 and missing_promises
                and not any(_is_promise(c.get("source_type", "late"))
                            for c in surviving)):
            c                 = max(missing_promises,
                                    key=lambda x: x.get("anchor_date") or "")
            anchor_date       = c.get("anchor_date") or today_str
            source_type       = c.get("source_type") or "late"
            outstanding, breakdown = self._promise_outstanding(
                anchor_date[:7], unit, today)
            disp = classify_status(rent, past_due)
            new_body = self.gcal._build_commitment_event(
                unit, anchor_date, source_type, outstanding,
                pm_notes=(
                    "PROMISED: [fill in, e.g. $500 or 'full balance']\n"
                    "NOTES:    [optional context]"
                ),
                source_status=disp, breakdown=breakdown,
            )
            try:
                created = _gcal_execute(self.gcal.service.events().insert(
                    calendarId=calendar_id, body=new_body))
                surviving.append({**c, "event_id": created["id"],
                                  "calendar_id": calendar_id})
                log.info(
                    f"  {bare_oid}: last promise was deleted — recreated one on "
                    f"{anchor_date} (a tracked unit keeps ≥1 promise until paid)")
            except HttpError as e:
                log.error(f"  {bare_oid}: failed to recreate last promise: {e}")

        self.state.set_commitments(soid, surviving)
