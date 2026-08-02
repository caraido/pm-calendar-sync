"""Sync orchestration: the per-run loop and per-unit / commitment logic."""
import re
from datetime import date, timedelta, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from .config import (
    FORCE_REFRESH, RENT_DUE_DAY, DEFAULT_LEASE_MONTHS,
    COMMITMENT_LOOKAHEAD_MONTHS, TIMEZONE, LATE_GRACE_DAYS,
    COMMITMENT_DIVIDER, log,
)
from .status import (
    classify_status, payment_status,
    STATUS_PAID, STATUS_UNPAID,
)
from .transforms import (
    normalize_tenant_name, build_group_property_map, build_tenant_info_map,
    build_payment_map, build_reversal_map, compute_running_balances,
    diff_rent_roll, format_address, parse_commitment_auto_section,
    unit_label, group_scope_key, group_display_name, _next_day,
    group_payments_by_day, resolve_collapse_transition,
)
from .appfolio import AppFolioClient
from .calendar_manager import GoogleCalendarManager, _gcal_execute
from .state import StateManager
from . import cache

# Month-entry format version.  fmt=2 = day-grouped payment events +
# settled-month collapse fields; an entry with any other value gets exactly
# one forced rebuild (regroup + collapse) and is stamped fmt=2.
STATE_FMT = 2


class SyncOrchestrator:

    def __init__(self):
        self.af    = AppFolioClient()
        self.gcal  = GoogleCalendarManager()
        self.state = StateManager()
        # Populated per run: bare occupancy_id → [(scope_key, calendar_id), ...]
        # for every property group the unit's property belongs to.  Multi-group
        # properties have >1 entry; used to mirror a promise created on one
        # group's calendar onto the unit's other group calendars.
        self._groups_by_oid: dict = {}
        # Commitment events created or converted DURING this run, event_id →
        # body.  Submit mode patches these into its pre-run calendar snapshot:
        # without the patch a commitment born after the listing would read as
        # PM-deleted and the ≥1-promise rule would spawn duplicates.
        self._fresh_commitments: dict = {}
        # Promise outcomes (kept / resolved) recorded DURING this run,
        # (soid, month) → [records].  _sync_unit's state rewrite merges its
        # own key; the run-tail flush persists whatever remains (submit mode
        # has no state rewrite).
        self._pending_promise_history: dict = {}

    # ── Top-level run (full sweep — the sole authority for correctness) ──────

    def run(self, mode: str = "full_nightly"):
        """
        Full convergent sweep over ALL active units.

        mode="full_nightly" (default — what plain `SyncOrchestrator().run()`
        does): pulls the tenant/property-group directories live (refreshing
        their cache), verifies every group calendar by id (healing AppFolio
        group renames), and re-asserts the PM ACL.

        mode="full": directories come from cache/ (live-pull fallback) and
        per-calendar ACL calls are skipped — the hourly backstop sweep.

        The first full run after deploy performs the one-time group cutover
        (see _run_group_cutover) before the sweep.
        """
        log.info(f"=== OKPM sync starting (mode: {mode}) ===")
        today      = datetime.now(ZoneInfo(TIMEZONE)).date()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)
        log.info(f"  Timezone: {TIMEZONE}, local date: {today}")

        log.info("Fetching rent_roll...")
        rent_roll = self.af.get_rent_roll()
        tenants, groups = self._load_directories(mode)
        log.info("Fetching tenant_ledger (current month)...")
        ledger = self.af.get_tenant_ledger_month(
            today.replace(day=1).isoformat(), today.isoformat())

        prop_to_group = build_group_property_map(groups)
        tenant_info   = build_tenant_info_map(tenants)
        payment_map   = build_payment_map(ledger)
        reversal_map  = build_reversal_map(ledger)
        active        = [r for r in rent_roll if r.get("status") == "Current"]
        log.info(f"  {len(active)} active leases, {len(payment_map)} with payments this month")
        if reversal_map:
            log.info(f"  {sum(len(v) for v in reversal_map.values())} NSF/negative "
                     f"reversal row(s) in the ledger")

        # ── Diagnostic: detect non-Current leases that might be missing ───
        non_current = [r for r in rent_roll if r.get("status") != "Current"]
        if non_current:
            status_counts = {}
            for r in non_current:
                s = r.get("status", "?")
                status_counts[s] = status_counts.get(s, 0) + 1
            log.info(f"  Skipped {len(non_current)} non-Current leases: {status_counts}")

        group_rows = self._group_rows_by_property_group(active, prop_to_group)
        if not self.state.migration_done("group_cutover_v1"):
            group_meta = self._run_group_cutover(
                group_rows, tenant_info, payment_map, today)
        else:
            group_meta = self._resolve_group_calendars(
                group_rows, use_cache=(mode != "full_nightly"))
        self._build_groups_by_oid(group_rows, group_meta)

        for scope_key, rows_and_groups in group_rows.items():
            group_name, calendar_id = group_meta[scope_key]
            log.info(f"Group: {group_name} ({len(rows_and_groups)} units)")
            # Group calendars are PM-only.  The PM ACL is re-asserted nightly;
            # other modes skip it except for a calendar created this very run.
            if mode == "full_nightly" or calendar_id in self.gcal.created_calendar_ids:
                self.gcal.ensure_pm_access(calendar_id)
            # Adopt PM copy-paste commitment copies BEFORE the unit loop so
            # this run's commitment pass treats them as tracked promises.
            try:
                self._adopt_untagged_commitments(
                    rows_and_groups, scope_key, calendar_id,
                    tenant_info, payment_map, today)
            except Exception as exc:
                log.error(f"  FAILED adoption scan for {group_name}: {exc}",
                          exc_info=True)
            for row, _ in rows_and_groups:
                try:
                    self._sync_unit(
                        row, calendar_id, due_date, today, this_month,
                        tenant_info, payment_map, scope_key=scope_key,
                        reversal_map=reversal_map,
                        deep_clean=(mode == "full_nightly"),
                    )
                except Exception as exc:
                    oid = row.get("occupancy_id", "?")
                    log.error(f"  FAILED unit {oid}: {exc}", exc_info=True)

        # Snapshot is written AFTER the sweep so a crashed run never advances
        # the update-mode diff baseline past what was actually applied.  (A
        # per-unit failure caught above still advances it — the next sweep
        # retries that unit anyway.)
        self._save_rent_roll_snapshot(rent_roll)
        self._flush_pending_promise_history()
        self.state.save()
        log.info("=== Sync complete ===")

    # ── Run-mode data plumbing ────────────────────────────────────────────────

    def _load_directories(self, mode: str) -> tuple:
        """Tenant + property-group directory rows.

        full_nightly pulls live and refreshes cache/directories.json; every
        other mode reads the cache and only falls back to a live pull (also
        re-writing the cache) when it is missing, corrupt, or pre-cutover
        (an old "owners"-shaped cache without property_groups).
        Returns (tenants, property_groups).
        """
        if mode != "full_nightly":
            cached = cache.load_json(cache.DIRECTORIES_FILE)
            if (cached is not None and "tenants" in cached
                    and "property_groups" in cached):
                log.info(
                    "Using cached directories "
                    f"(refreshed {cached.get('refreshed_at', '?')})")
                return cached["tenants"], cached["property_groups"]
        log.info("Fetching tenant_directory...")
        tenants = self.af.get_tenant_directory()
        log.info("Fetching property_group_directory...")
        groups = self.af.get_property_group_directory()
        cache.save_json(cache.DIRECTORIES_FILE, {
            "refreshed_at":    datetime.utcnow().isoformat(),
            "tenants":         tenants,
            "property_groups": groups,
        })
        return tenants, groups

    def _save_rent_roll_snapshot(self, rent_roll: list):
        """Persist the raw rent_roll rows — the diff baseline for update mode
        and the balance source for submit mode."""
        cache.save_json(cache.RENT_ROLL_FILE, {
            "refreshed_at": datetime.utcnow().isoformat(),
            "rows":         rent_roll,
        })

    def _group_rows_by_property_group(self, active: list,
                                      prop_to_group: dict) -> dict:
        """scope_key ("g{gid}") → [(rent_roll row, group dict), ...] for every
        active lease.  A property in several groups contributes its rows to
        each — the unit then appears on every one of its groups' calendars
        (the same multi-calendar model co-ownership used to drive)."""
        group_rows: dict = {}
        for row in active:
            pid = row.get("property_id")
            groups_list = prop_to_group.get(pid)
            if not groups_list:
                # Try string↔int coercion in case of type mismatch
                alt_pid = int(pid) if isinstance(pid, str) and pid.isdigit() else str(pid)
                groups_list = prop_to_group.get(alt_pid)
                if groups_list:
                    log.warning(
                        f"  property_id type mismatch for {row.get('property_name','?')}: "
                        f"rent_roll has {type(pid).__name__}({pid!r}), "
                        f"group_map expects {type(alt_pid).__name__}")
            if groups_list:
                for grp in groups_list:
                    group_rows.setdefault(
                        group_scope_key(grp["group_id"]), []).append((row, grp))
            else:
                # Expected state, not an error: unassigned properties are
                # intentionally unsynced (no calendar).
                log.info(
                    f"  UNGROUPED: property_id={pid!r} "
                    f"({row.get('property_name','?')}, "
                    f"tenant={row.get('tenant_name','?')}) — not in any "
                    f"property group, no calendar")
        return group_rows

    def _resolve_group_calendars(self, group_rows: dict, use_cache: bool) -> dict:
        """Resolve every property group's calendar up front.

        A multi-group property's units appear on all of its groups'
        calendars.  Resolving every calendar before the sync loop lets each
        unit know its sibling calendars, so a promise dragged on one group's
        calendar can be mirrored onto the others in the same run.

        use_cache=True consults state's `_calendars` map (keys "g{gid}"),
        skipping the calendarList() pagination for known groups; a miss falls
        through to a live resolve (creating the PM-only calendar if needed).
        The nightly sweep passes use_cache=False and verifies each cached id
        live BY ID — patching the summary when the group was renamed in
        AppFolio — instead of re-resolving by name, which would orphan the
        calendar on any rename.  Returns scope_key → (group_name, calendar_id).
        """
        group_meta: dict = {}
        for scope_key, rows_and_groups in group_rows.items():
            group       = rows_and_groups[0][1]
            group_name  = group_display_name(group)
            calendar_id = self.state.get_calendar_id(scope_key)
            if calendar_id and not use_cache:
                if not self.gcal.ensure_calendar_summary(calendar_id, group_name):
                    log.warning(
                        f"  Calendar for {group_name} ({calendar_id}) is "
                        f"gone — recreating")
                    calendar_id = None
            if not calendar_id:
                calendar_id = self.gcal.get_or_create_group_calendar(group_name)
            self.state.set_calendar_id(scope_key, calendar_id)
            group_meta[scope_key] = (group_name, calendar_id)
        return group_meta

    def _build_groups_by_oid(self, group_rows: dict, group_meta: dict):
        """Populate self._groups_by_oid (bare oid → [(scope_key, calendar_id)]).
        Required by _mirror_commitment_to_siblings — every run mode that can
        create or discover commitments must call this."""
        self._groups_by_oid = {}
        for scope_key, rows_and_groups in group_rows.items():
            calendar_id = group_meta[scope_key][1]
            for row, _ in rows_and_groups:
                oid = str(row.get("occupancy_id"))
                self._groups_by_oid.setdefault(oid, []).append(
                    (scope_key, calendar_id))

    # ── One-time group cutover (owner calendars → property-group calendars) ──

    def _run_group_cutover(self, group_rows: dict, tenant_info: dict,
                           payment_map: dict, today: date) -> dict:
        """One-time migration from per-owner to per-property-group calendars.

        Phases (each idempotent — a crash anywhere resumes on the next full
        run; the marker is only written after every phase fully succeeds):
          1. Retire the legacy owner calendars ([RETIRED] rename + strip
             non-PM ACLs) FIRST, so a group named after an owner can never
             adopt a legacy calendar by summary match in phase 2.
          2. Create/resolve the group calendars live (PM-only sharing).
          3. Rebuild every active commitment on its unit's group calendar(s)
             — PM notes recovered from the old event — then delete the old
             event and legacy registry key.  Duplicate protection is the
             LIVE target calendar (same anchor date + source type), not just
             the registry, so a crash between insert and state.save cannot
             double promises on retry.
          4. Move `_calendars` owner entries to `_retired_calendars`, purge
             legacy owner-scoped state, write the marker, save state NOW
             (the sweep that follows takes many minutes).

        Marker: _migrations["group_cutover_v1"].  Returns group_meta so
        run() skips a second resolve.
        """
        log.info("=== GROUP CUTOVER: reorganizing calendars by property group ===")

        # ── Phase 1: retire legacy owner calendars ────────────────────────
        legacy_owner_ids = [k for k in self.state.data["_calendars"]
                            if not str(k).startswith("g")]
        distinct_cals: dict = {}   # several owners can share one calendar
        for owner_id in legacy_owner_ids:
            distinct_cals.setdefault(
                self.state.data["_calendars"][owner_id], owner_id)
        retire_failed = 0
        for cal_id in distinct_cals:
            if not self.gcal.retire_calendar(cal_id):
                retire_failed += 1
        log.info(f"  Retired {len(distinct_cals) - retire_failed}/"
                 f"{len(distinct_cals)} legacy owner calendar(s)")

        # ── Phase 2: create/resolve group calendars ───────────────────────
        group_meta = self._resolve_group_calendars(group_rows, use_cache=False)

        # ── Phase 3: migrate active commitments ───────────────────────────
        migrated, unmigrated = self._migrate_legacy_commitments(
            group_rows, group_meta, tenant_info, payment_map, today)

        # ── Phase 4: bookkeeping — only when nothing is left pending ─────
        pending_comms = [
            k for k, v in self.state.data["_commitments"].items()
            if self.state._LEGACY_COMM_KEY.match(k) and v
        ]
        if retire_failed or pending_comms:
            log.warning(
                f"  Cutover incomplete ({retire_failed} retirement(s) failed, "
                f"{len(pending_comms)} legacy commitment key(s) pending) — "
                f"marker NOT written; the next full run resumes")
            self.state.save()
            return group_meta
        for owner_id in legacy_owner_ids:
            self.state.retire_calendar_entry(owner_id)
        purged = self.state.purge_legacy_owner_entries()
        self.state.mark_migration_done("group_cutover_v1", {
            "retired_calendars":     len(distinct_cals),
            "migrated_commitments":  migrated,
            "unmigrated":            unmigrated,
            "purged_entries":        purged,
        })
        self.state.save()
        log.info(f"=== GROUP CUTOVER complete: {len(distinct_cals)} calendar(s) "
                 f"retired, {migrated} commitment(s) migrated, "
                 f"{purged} legacy state entr(y/ies) purged ===")
        return group_meta

    def _migrate_legacy_commitments(self, group_rows: dict, group_meta: dict,
                                    tenant_info: dict, payment_map: dict,
                                    today: date) -> tuple:
        """Rebuild every legacy ({oid}@{owner_id}) commitment on the unit's
        group calendar(s), then delete the old event + legacy key.  Returns
        (migrated_count, unmigrated_entries).  A per-event Google failure
        keeps the legacy key so the next full run retries; the live-calendar
        duplicate oracle makes the retry safe."""
        oid_to_scopes: dict = {}
        row_by_oid: dict = {}
        for scope_key, rows_and_groups in group_rows.items():
            cal_id = group_meta[scope_key][1]
            for row, _ in rows_and_groups:
                o = str(row.get("occupancy_id"))
                oid_to_scopes.setdefault(o, []).append((scope_key, cal_id))
                row_by_oid[o] = row

        legacy_keys = [k for k, v in self.state.data["_commitments"].items()
                       if self.state._LEGACY_COMM_KEY.match(k) and v]
        migrated = 0
        unmigrated: list = []
        today_month = today.strftime("%Y-%m")
        listing_cache: dict = {}

        def _target_commitments(cal_id: str, oid: str) -> list:
            key = (cal_id, oid)
            if key not in listing_cache:
                listing_cache[key] = self.gcal.find_all_events_by_type(
                    cal_id, oid, "commitment")
            return listing_cache[key]

        for legacy_key in legacy_keys:
            oid = legacy_key.split("@")[0]
            targets = oid_to_scopes.get(oid)
            if not targets:
                # Lease no longer Current or property ungrouped: the promise
                # has no home calendar.  The old event stays frozen on the
                # retired calendar; record it for the PM and drop the key.
                for c in self.state.data["_commitments"][legacy_key]:
                    unmigrated.append({**c, "legacy_key": legacy_key})
                log.warning(
                    f"  {oid}: {len(self.state.data['_commitments'][legacy_key])} "
                    f"commitment(s) have no group calendar (lease gone or "
                    f"property ungrouped) — left frozen on the retired calendar")
                del self.state.data["_commitments"][legacy_key]
                continue

            unit = self._make_unit(row_by_oid[oid], tenant_info, payment_map)
            all_ok = True
            for c in list(self.state.data["_commitments"][legacy_key]):
                anchor       = c.get("anchor_date") or today.isoformat()
                source       = c.get("source_type") or "late"
                covers       = c.get("covers_rent_month")
                origin_month = c.get("origin_month") or anchor[:7]

                # Recover PM notes from the old event (best effort).
                pm_notes = ""
                old_cal, old_eid = c.get("calendar_id"), c.get("event_id")
                if old_cal and old_eid:
                    old_ev = self.gcal.get_event(old_cal, old_eid)
                    desc   = (old_ev or {}).get("description") or ""
                    if COMMITMENT_DIVIDER in desc:
                        pm_notes = desc.split(COMMITMENT_DIVIDER)[0].rstrip()

                for scope_key, target_cal in targets:
                    new_soid = f"{oid}@{scope_key}"
                    if any((x.get("source_type") or "late") == source
                           and (x.get("anchor_date") or "") == anchor
                           for x in self.state.get_commitments(new_soid)):
                        continue   # already migrated (this or a prior attempt)
                    entry = {
                        "anchor_date":       anchor,
                        "source_type":       source,
                        "origin_month":      origin_month,
                        "calendar_id":       target_cal,
                        "covers_rent_month": covers,
                    }
                    try:
                        # Live-calendar oracle: an event inserted by a crashed
                        # attempt is adopted, never duplicated.
                        dup = next(
                            (ev for ev in _target_commitments(target_cal, oid)
                             if ev.get("start", {}).get("date") == anchor
                             and ev.get("extendedProperties", {})
                                  .get("private", {})
                                  .get("okpm_source_type") == source),
                            None)
                        if dup is not None:
                            self.state.add_commitment(
                                new_soid, {**entry, "event_id": dup["id"]})
                            continue
                        # Same display computation as
                        # _mirror_commitment_to_siblings.
                        breakdown = None
                        if source in ("status", "payment", "late"):
                            outstanding, breakdown = self._promise_outstanding(
                                anchor[:7], unit, today)
                            disp = classify_status(unit["rent"], unit["past_due"])
                        else:
                            outstanding = unit["past_due"] + (
                                unit["rent"] if (covers and covers > today_month)
                                else 0.0)
                            disp = ""
                        body = self.gcal._build_commitment_event(
                            unit, anchor, source, max(0.0, outstanding),
                            pm_notes=pm_notes, source_status=disp,
                            breakdown=breakdown)
                        created = _gcal_execute(self.gcal.service.events().insert(
                            calendarId=target_cal, body=body))
                    except HttpError as e:
                        log.error(f"  {oid}: failed to migrate {source} promise "
                                  f"({anchor}) to {target_cal}: {e}")
                        all_ok = False
                        continue
                    self._fresh_commitments[created["id"]] = created
                    self.state.add_commitment(
                        new_soid, {**entry, "event_id": created["id"]})
                    migrated += 1
                    log.info(f"  {oid}: migrated {source} promise ({anchor}) "
                             f"→ group calendar {target_cal}")

                # Old event goes away only after every target has its copy —
                # the promise must never exist on zero calendars.
                if all_ok and old_cal and old_eid:
                    try:
                        self.gcal.delete_event(old_cal, old_eid)
                    except HttpError as e:
                        if e.resp.status != 404:
                            log.warning(f"  {oid}: could not delete old "
                                        f"commitment event {old_eid}: {e}")
            if all_ok:
                del self.state.data["_commitments"][legacy_key]
        return migrated, unmigrated

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
        if not self.state.migration_done("group_cutover_v1"):
            log.warning(
                "Group cutover has not run yet — deferring to the next full "
                "sweep (≤1h); no changes made")
            return
        today      = datetime.now(ZoneInfo(TIMEZONE)).date()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)
        log.info(f"  Timezone: {TIMEZONE}, local date: {today}")

        tenants, groups = self._load_directories("update")
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

        prop_to_group = build_group_property_map(groups)
        tenant_info   = build_tenant_info_map(tenants)
        payment_map   = build_payment_map(ledger)
        reversal_map  = build_reversal_map(ledger)
        active        = [r for r in rent_roll if r.get("status") == "Current"]

        # Group + resolve for ALL active rows, not just changed ones: the
        # sibling map must be complete for _mirror_commitment_to_siblings.
        group_rows = self._group_rows_by_property_group(active, prop_to_group)
        group_meta = self._resolve_group_calendars(group_rows, use_cache=True)
        self._build_groups_by_oid(group_rows, group_meta)

        synced = 0
        for scope_key, rows_and_groups in group_rows.items():
            group_name, calendar_id = group_meta[scope_key]
            # ACL only for a calendar created this very run (brand-new group).
            if calendar_id in self.gcal.created_calendar_ids:
                self.gcal.ensure_pm_access(calendar_id)
            scoped = [(row, grp) for row, grp in rows_and_groups
                      if str(row.get("occupancy_id")) in changed]
            if not scoped:
                continue
            log.info(f"Group: {group_name} ({len(scoped)} changed unit(s))")
            for row, _ in scoped:
                try:
                    self._sync_unit(
                        row, calendar_id, due_date, today, this_month,
                        tenant_info, payment_map, scope_key=scope_key,
                        reversal_map=reversal_map,
                    )
                    synced += 1
                except Exception as exc:
                    oid = row.get("occupancy_id", "?")
                    log.error(f"  FAILED unit {oid}: {exc}", exc_info=True)

        # Even a zero-change run advances the snapshot: the baseline must
        # always reflect the last examined rent_roll.
        self._save_rent_roll_snapshot(rent_roll)
        self._flush_pending_promise_history()
        self.state.save()
        log.info(f"=== Update complete: {synced} unit sync(s) ===")

    # ── Submit mode (drag/commitment consolidation, no AppFolio) ─────────────

    def run_submit(self):
        """
        Manual fast path: consolidate PM calendar drags into commitments using
        ONLY cached data — no AppFolio call.  Per calendar, ONE unbounded
        events().list supplies every event; per unit we run drag detection
        (status / payment / kickstart), the commitment lifecycle, and
        placeholder absorption.  Status/payment/placeholder events are NOT
        rebuilt here, and balances (thus commitment title amounts) come from
        the cached rent_roll snapshot — at most ~1h stale; the next full
        sweep trues everything up.  FORCE_REFRESH is ignored in this mode.
        """
        log.info("=== OKPM sync starting (mode: submit) ===")
        if not self.state.migration_done("group_cutover_v1"):
            log.warning(
                "Group cutover has not run yet — deferring to the next full "
                "sweep (≤1h); no changes made")
            return
        today      = datetime.now(ZoneInfo(TIMEZONE)).date()
        this_month = today.strftime("%Y-%m")
        log.info(f"  Timezone: {TIMEZONE}, local date: {today}")
        self._fresh_commitments = {}
        self._pending_promise_history = {}

        snap = cache.load_json(cache.RENT_ROLL_FILE)
        rows = snap.get("rows") if snap else None
        if isinstance(rows, list):
            log.info(
                "  Using cached rent_roll "
                f"(refreshed {snap.get('refreshed_at', '?')})")
        else:
            # One-time live pull; also heals the cache for the next submit.
            log.warning("  No usable rent_roll snapshot — pulling live rent_roll once")
            rows = self.af.get_rent_roll()
            self._save_rent_roll_snapshot(rows)

        tenants, groups = self._load_directories("submit")

        prop_to_group = build_group_property_map(groups)
        tenant_info   = build_tenant_info_map(tenants)
        active        = [r for r in rows if r.get("status") == "Current"]
        log.info(f"  {len(active)} active leases (from snapshot)")

        group_rows = self._group_rows_by_property_group(active, prop_to_group)
        group_meta = self._resolve_group_calendars(group_rows, use_cache=True)
        self._build_groups_by_oid(group_rows, group_meta)

        for scope_key, rows_and_groups in group_rows.items():
            group_name, calendar_id = group_meta[scope_key]
            log.info(f"Group: {group_name} ({len(rows_and_groups)} units)")
            # ACL only for a calendar created this very run (brand-new group).
            if calendar_id in self.gcal.created_calendar_ids:
                self.gcal.ensure_pm_access(calendar_id)
            events_by_oid = self.gcal.list_all_events(calendar_id)
            # Adopt PM copy-paste commitment copies.  Uses the same q= scan
            # as the full sweep (list_all_events deliberately skips untagged
            # events); adopted ids land in _fresh_commitments so this run's
            # commitment pass sees them despite the pre-adoption listing.
            try:
                self._adopt_untagged_commitments(
                    rows_and_groups, scope_key, calendar_id,
                    tenant_info, {}, today)
            except Exception as exc:
                log.error(f"  FAILED adoption scan for {group_name}: {exc}",
                          exc_info=True)
            for row, _ in rows_and_groups:
                oid  = str(row.get("occupancy_id"))
                soid = f"{oid}@{scope_key}"
                try:
                    # No ledger in this mode → payments=[] / amount_paid=0.
                    # The commitment builders only read balance + identity
                    # fields, all present in the snapshot row.
                    unit = self._make_unit(row, tenant_info, {})
                    unit_events = events_by_oid.get(oid, [])
                    self._detect_and_convert_drags(
                        soid, calendar_id, unit, today, this_month,
                        unit_events)
                    self._process_commitments(
                        soid, calendar_id, unit, today,
                        has_known_or_new=True,
                        events=self._commitment_events_for(soid, unit_events))
                    self._absorb_promised_placeholders(soid, calendar_id, today)
                except Exception as exc:
                    log.error(f"  FAILED unit {oid}: {exc}", exc_info=True)

        self._flush_pending_promise_history()
        self.state.save()
        log.info("=== Submit complete ===")

    def _commitment_events_for(self, soid: str, unit_events: list) -> list:
        """This unit's commitment-typed events for _process_commitments: the
        pre-listed commitment events plus any commitment created or converted
        DURING this run (in-place conversions, snap-back spawns, co-owner
        mirrors) that the pre-run listing cannot contain.  Without the patch
        those would read as PM-deleted and the ≥1-promise rule would spawn
        duplicates."""
        evs = [
            ev for ev in unit_events
            if (ev.get("extendedProperties", {}).get("private", {})
                .get("okpm_event_type")) == "commitment"
        ]
        listed = {ev["id"] for ev in evs}
        for c in self.state.get_commitments(soid):
            eid = c.get("event_id")
            if eid and eid not in listed and eid in self._fresh_commitments:
                evs.append(self._fresh_commitments[eid])
                listed.add(eid)
        return evs

    def _detect_and_convert_drags(
        self, soid: str, calendar_id: str, unit: dict,
        today: date, this_month: str, unit_events: list,
    ):
        """
        Submit-mode drag pass over one unit, driven entirely by the
        pre-listed events (no per-event Google reads).  Composes the same
        primitives — and gates — as the full sweep.  Submit has no ledger, so
        sorted_payments is always empty and the sweep's suppress_kickstart
        reduces to "a commitment already covers this month".
          1. status event dragged forward → in-place conversion (site 1);
             submit persists the status_event_id=None state change itself
             (in the sweep, _sync_unit's tail rewrite does it)
          2. otherwise the locked status event is verified → new commitment
             + snap-back, or plain revert
          3. locked payment markers → same; canonical dates come from state's
             payment_event_dates (entries predating that key skip payment
             drags until a full/update run backfills them)
          4. kickstart placeholders in the lookahead window → conversion
        """
        events_by_id = {ev["id"]: ev for ev in unit_events}
        oid      = soid.split("@")[0] if "@" in soid else soid
        rent     = unit["rent"]
        past_due = unit["past_due"]
        status   = classify_status(rent, past_due)
        due_date = date(today.year, today.month, RENT_DUE_DAY)

        prior = self.state.get(soid, this_month)
        # Distrust state written for a DIFFERENT calendar (as in _sync_unit).
        if prior and prior.get("calendar_id") != calendar_id:
            prior = None

        self.state.migrate_bare_commitments(oid, soid, calendar_id)
        self.state.deduplicate_commitments(soid)
        commitments = self.state.get_commitments(soid)
        commitment_months = {
            c["covers_rent_month"] for c in commitments
            if c.get("covers_rent_month")
        }

        # ── 1+2: status event ────────────────────────────────────────────
        if prior and prior.get("status_event_id"):
            canonical = prior.get("status_event_date", due_date.isoformat())
            converted = False
            if this_month not in commitment_months:
                converted = self._convert_status_drag(
                    soid, calendar_id, unit, prior["status_event_id"],
                    canonical, today, this_month, commitment_months,
                    source_status=status, events_by_id=events_by_id)
                if converted:
                    self.state.set(soid, this_month,
                                   {**prior, "status_event_id": None})
            if not converted:
                # Unmoved → no-op; moved backward → revert.  The forward-
                # with-no-covering-commitment case was consumed above, so
                # this can only create a commitment when one already exists
                # for another reason — i.e. never (same net as the sweep).
                self._detect_status_snapback(
                    soid, calendar_id, prior["status_event_id"], canonical,
                    unit, today, this_month, past_due, status,
                    commitment_months, events_by_id=events_by_id)

        # ── 3: payment markers ───────────────────────────────────────────
        if prior and prior.get("payment_event_ids"):
            dates = prior.get("payment_event_dates")
            if dates is None:
                log.info(
                    f"  {oid}: state entry predates payment_event_dates — "
                    f"payment drags wait for the next full/update run")
            else:
                self._detect_payment_drags(
                    soid, calendar_id, prior["payment_event_ids"], dates,
                    unit, today, this_month, past_due, status,
                    commitment_months, events_by_id=events_by_id)

        # ── 4: kickstart placeholders in the lookahead window ────────────
        commitments = self.state.get_commitments(soid)   # 1–3 may have added
        start   = due_date + timedelta(days=32)
        horizon = start + timedelta(days=32 * COMMITMENT_LOOKAHEAD_MONTHS)
        for i, fdue in enumerate(self._month_range(start, horizon)):
            if i >= COMMITMENT_LOOKAHEAD_MONTHS:
                break
            fmonth  = fdue.strftime("%Y-%m")
            prior_f = self.state.get(soid, fmonth)
            if prior_f and prior_f.get("calendar_id") != calendar_id:
                prior_f = None
            if not prior_f:
                continue
            # Same skip gates as the sweep's future-months loop.
            if any((c.get("source_type") == "kickstart"
                    and c.get("origin_month") == fmonth)
                   or c.get("covers_rent_month") == fmonth
                   for c in commitments):
                continue
            if any(c.get("source_type") in ("status", "payment", "late")
                   and (c.get("anchor_date") or "")[:7] >= fmonth
                   for c in commitments):
                continue
            if prior_f.get("rent_event_id") and not prior_f.get("is_commitment"):
                self._convert_kickstart_drag(
                    soid, calendar_id, unit, fmonth, fdue.isoformat(),
                    prior_f, today, events_by_id=events_by_id)

    def _absorb_promised_placeholders(
        self, soid: str, calendar_id: str, today: date,
    ):
        """
        Submit-mode mirror of the future-months absorption step in _sync_unit
        (keep the two in sync): a status/payment/late promise anchored in (or
        beyond) a future month folds that month's rent into its combined
        total, so the month's placeholder is deleted while the promise
        stands.  The full sweep recreates the placeholder if the promise
        later resolves or is dragged back.
        """
        oid         = soid.split("@")[0] if "@" in soid else soid
        commitments = self.state.get_commitments(soid)
        promise_months = [
            (c.get("anchor_date") or "")[:7]
            for c in commitments
            if c.get("source_type") in ("status", "payment", "late")
        ]
        max_month = max([m for m in promise_months if m], default="")
        if not max_month:
            return
        try:
            end = date(int(max_month[:4]), int(max_month[5:7]), RENT_DUE_DAY)
        except (ValueError, IndexError):
            return
        due_date = date(today.year, today.month, RENT_DUE_DAY)
        for fdue in self._month_range(due_date + timedelta(days=32), end):
            fmonth  = fdue.strftime("%Y-%m")
            prior_f = self.state.get(soid, fmonth)
            if prior_f and prior_f.get("calendar_id") != calendar_id:
                prior_f = None
            if not (prior_f and prior_f.get("rent_event_id")
                    and not prior_f.get("is_commitment")):
                continue
            # Same gate order as the sweep: a kickstart covering the month
            # wins over absorption.
            if any((c.get("source_type") == "kickstart"
                    and c.get("origin_month") == fmonth)
                   or c.get("covers_rent_month") == fmonth
                   for c in commitments):
                continue
            if not any(c.get("source_type") in ("status", "payment", "late")
                       and (c.get("anchor_date") or "")[:7] >= fmonth
                       for c in commitments):
                continue
            self.gcal.delete_event(calendar_id, prior_f["rent_event_id"])
            self.state.set(soid, fmonth, {**prior_f, "rent_event_id": None})
            log.info(f"  {oid}: promise absorbs {fmonth} placeholder — removed")

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
        Mirror a promise onto the unit's sibling group calendars.

        A property in several property groups shows up on one calendar per
        group.  When the PM drags an event on ONE group's calendar, the
        promise is created only there; without this, the other groups never
        see it.  For every OTHER group containing the same occupancy, create
        an equivalent commitment event on that group's calendar and register
        it in that scope's commitment state.  Each sibling scope's own
        `_sync_unit` then suppresses that month's status/placeholder normally
        (this run if not yet processed, next run otherwise) and thereafter
        treats the promise as its own.

        Idempotent: a sibling that already carries a commitment with the same
        source_type + anchor_date is skipped, so repeated runs, the sibling's own
        rediscovery, and split copies never spawn duplicates.
        """
        siblings = self._groups_by_oid.get(oid, [])
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

        for scope_key, cal_id in siblings:
            if cal_id == origin_cal:
                continue
            sib_soid = f"{oid}@{scope_key}"
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

            self._fresh_commitments[created["id"]] = created
            self.state.add_commitment(sib_soid, {
                "event_id":          created["id"],
                "anchor_date":       anchor,
                "source_type":       source,
                "origin_month":      origin_month,
                "calendar_id":       cal_id,
                "covers_rent_month": covers,
            })
            log.info(
                f"  {oid}: mirrored {source} promise on {anchor} to sibling "
                f"group calendar {cal_id}")

    def _adopt_untagged_commitments(
        self, rows_and_groups: list, scope_key, calendar_id: str,
        tenant_info: dict, payment_map: dict, today: date,
    ) -> int:
        """
        Adopt PM copy-paste commitment copies (split plans).  The Calendar UI
        drops extendedProperties.private on copy, so copies are invisible to
        every okpm_* locator — never updated, never resolved, frozen at the
        copied description.  This scan finds them (divider text, no okpm
        tag), attributes each to a unit via the auto section's Tenant line
        (skipping — never guessing — on ambiguity), rebuilds the body in
        place (which restores the okpm tags), and registers + mirrors the
        promise exactly like a discovered split copy.  Runs BEFORE the
        per-unit loop so the same run's commitment pass treats the adoptee
        as a tracked promise.  Returns the number adopted.
        """
        copies = self.gcal.find_untagged_commitment_copies(calendar_id)
        if not copies:
            return 0
        adopted     = 0
        today_month = today.strftime("%Y-%m")
        for ev in copies:
            parsed = parse_commitment_auto_section(ev.get("description") or "")
            if not parsed or not parsed["tenant"]:
                log.warning(
                    f"  Untagged commitment copy {ev.get('id')} on "
                    f"{calendar_id}: auto section unparseable — skipping")
                continue
            matches = [row for row, _ in rows_and_groups
                       if normalize_tenant_name(row.get("tenant", ""))
                       == parsed["tenant"]]
            if len(matches) > 1:
                # Same tenant name on multiple units — require the address
                # (and unit label, when present) to appear in the copy.
                matches = [row for row in matches
                           if format_address(row) in parsed["auto_section"]
                           and (not unit_label(row)
                                or unit_label(row) in parsed["auto_section"])]
            if len(matches) != 1:
                log.warning(
                    f"  Untagged commitment copy {ev.get('id')}: tenant "
                    f"{parsed['tenant']!r} matched {len(matches)} unit(s) on "
                    f"this calendar — skipping (never guess)")
                continue
            row  = matches[0]
            oid  = str(row.get("occupancy_id"))
            soid = f"{oid}@{scope_key}"
            unit = self._make_unit(row, tenant_info, payment_map)

            start  = ev.get("start", {})
            anchor = start.get("date") or start.get("dateTime", "")[:10]
            if not anchor:
                log.warning(
                    f"  Untagged commitment copy {ev.get('id')}: "
                    f"no start date — skipping")
                continue
            source_type = parsed["source_type"]
            # covers_rent_month: byte-identical rules to the split-copy
            # discovery loop in _process_commitments.
            covers = (
                anchor[:7] if (source_type == "late" and anchor[:7] > today_month)
                else anchor[:7] if source_type == "kickstart"
                else today_month if source_type in ("status", "payment")
                else None
            )
            # Same display computation as _mirror_commitment_to_siblings.
            breakdown = None
            if source_type in ("status", "payment", "late"):
                outstanding, breakdown = self._promise_outstanding(
                    anchor[:7], unit, today)
                disp = classify_status(unit["rent"], unit["past_due"])
            else:
                outstanding = unit["past_due"] + (
                    unit["rent"] if (covers and covers > today_month) else 0.0)
                disp = ""
            new_body = self.gcal._build_commitment_event(
                unit, anchor, source_type, max(0.0, outstanding),
                pm_notes=parsed["pm_notes"], source_status=disp,
                breakdown=breakdown)
            # Tag FIRST: registering an id the okpm listing cannot see would
            # make _process_commitments read it as PM-deleted and the
            # ≥1-promise rule would spawn a duplicate promise.
            try:
                _gcal_execute(self.gcal.service.events().update(
                    calendarId=calendar_id, eventId=ev["id"], body=new_body))
            except HttpError as e:
                log.error(
                    f"  {oid}: failed to adopt commitment copy {ev['id']}: {e}")
                continue
            _commit = {
                "event_id":          ev["id"],
                "anchor_date":       anchor,
                "source_type":       source_type,
                "origin_month":      anchor[:7],
                "calendar_id":       calendar_id,
                "covers_rent_month": covers,
            }
            self._fresh_commitments[ev["id"]] = {**new_body, "id": ev["id"]}
            self.state.add_commitment(soid, _commit)
            self._mirror_commitment_to_siblings(oid, _commit, unit, today)
            adopted += 1
            log.info(
                f"  {oid}: adopted untagged commitment copy {ev['id']} "
                f"(anchor {anchor}, source {source_type})")
        return adopted

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
        written = self.gcal.convert_to_commitment(
            calendar_id, status_event_id, unit, live, "status",
            max(0.0, past_due), source_status=source_status)
        self._fresh_commitments[status_event_id] = {**written, "id": status_event_id}
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
                self._fresh_commitments[created["id"]] = created
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
                        self._fresh_commitments[created["id"]] = created
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
                written = self.gcal.convert_to_commitment(
                    calendar_id, placeholder_id, unit,
                    live_date, "kickstart", max(0.0, past_due),
                    source_status=STATUS_UNPAID,
                )
                self._fresh_commitments[placeholder_id] = {
                    **written, "id": placeholder_id}
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
        scope_key: str = "",
        reversal_map: Optional[dict] = None,
        deep_clean: bool = False,
    ):
        unit     = self._make_unit(row, tenant_info, payment_map)
        oid      = unit["occupancy_id"]   # bare — for Google Calendar
        soid     = f"{oid}@{scope_key}" if scope_key else oid  # for state keys
        rent     = unit["rent"]
        past_due = unit["past_due"]
        status   = classify_status(rent, past_due)

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

        prior = self.state.get(soid, this_month)

        # Distrust state written for a DIFFERENT calendar.
        if prior and prior.get("calendar_id") != calendar_id:
            prior = None

        # Captured BEFORE the current-month state overwrite below: surplus
        # payment-event ids (their ledger rows vanished — reversed payments
        # usually disappear from the pull) and the stored payment records
        # are what the NSF-reversal pass matches against.
        prior_payment_ids = list(prior.get("payment_event_ids") or []) if prior else []
        prior_payments    = list(prior.get("payments") or []) if prior else []
        # Reversals already applied to THIS month (carried through rewrites);
        # their notes re-render onto the status event on every rebuild.
        carried_reversals = list(prior.get("nsf_reversals_applied") or []) if prior else []
        carried_nsf_ids   = list(prior.get("nsf_event_ids") or []) if prior else []
        reversal_notes    = [self._format_reversal_note(r) for r in carried_reversals]
        carried_promise_history = (
            list(prior.get("promise_history") or []) if prior else [])

        # ── Settled-month collapse state machine ─────────────────────────────
        # A fully-paid month renders as ONE green/pink event on the last
        # payment date (payment + promise history live in its description);
        # the per-payment events are deleted by the cleanup pass below.  All
        # display below runs on same-day GROUPS (one event per payment
        # date), while state `payments` stays per ledger row.
        day_groups  = group_payments_by_day(sorted_payments)
        fmt_migrate = bool(prior) and prior.get("fmt") != STATE_FMT
        collapse    = resolve_collapse_transition(prior, sorted_payments, past_due)
        collapsed   = collapse["state"] == "collapsed"
        frozen      = collapse["state"] == "frozen"
        reactivated = collapse["state"] == "reactivated"
        if collapse["reverted"]:
            log.info(
                f"  {oid}: settled collapse REVERTED — a settled payment "
                f"vanished or bounced; re-expanding {this_month}")
        if collapse.get("healed"):
            log.info(
                f"  {oid}: bogus zero-payment settlement HEALED — empty "
                f"settled snapshot (nothing was ever settled); re-evaluating "
                f"{this_month} from the live balance")
        if reactivated:
            render_groups = group_payments_by_day(collapse["fresh"])
        elif collapsed or frozen:
            render_groups = []
        else:
            render_groups = day_groups
        balances = compute_running_balances(render_groups, past_due)

        # settled_on: stamped at (re-)collapse, carried while settled.
        if collapsed:
            if (prior and prior.get("collapse_state") in ("collapsed", "frozen")
                    and prior.get("settled_on")):
                settled_on = prior["settled_on"]
            else:
                settled_on = today.isoformat()
            settled_past_due = past_due
        elif frozen or reactivated:
            settled_on       = prior.get("settled_on") if prior else None
            settled_past_due = prior.get("settled_past_due") if prior else None
        else:
            settled_on = settled_past_due = None

        # REACTIVATED: the settled payments render as description history on
        # the fresh-cycle status event, never as events again.
        settled_prefix = None
        if reactivated:
            srows = collapse["settled_rows"]
            settled_prefix = {
                "count":      len(srows),
                "total":      sum(r["amount"] for r in srows
                                  if not r.get("is_nsf")),
                "settled_on": settled_on or "",
                "rows":       srows,
            }

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

        # One unbounded commitment listing per unit (moved up from
        # _process_commitments — same call, same volume) so the projection
        # below and the absorption pass reason from the SAME live anchors;
        # registry anchors are only the fallback for already-gone events.
        live_commitments = self.gcal.find_all_events_by_type(
            calendar_id, oid, "commitment")
        live_anchor_by_id = {}
        for _ev in live_commitments:
            _start = _ev.get("start", {})
            live_anchor_by_id[_ev["id"]] = (
                _start.get("date") or _start.get("dateTime", "")[:10])

        # Promise outcomes visible to THIS run's event descriptions:
        # history already persisted + a stateless projection of what this
        # run's commitment pass will record (it runs after the status-event
        # build, so without the projection a settled month would list its
        # resolved promises one run late).  Only payments dated today or
        # earlier count — a post-dated ledger row hasn't "kept" anything yet.
        promise_payment_dates = {
            p["date"] for p in sorted_payments
            if not p.get("is_nsf") and p["date"] <= today.isoformat()}
        projected_history = self._merge_promise_history(
            carried_promise_history,
            self._project_promise_outcomes(
                commitments, calendar_id, promise_payment_dates, past_due,
                live_anchor_by_id=live_anchor_by_id))

        # ── Status event date ─────────────────────────────────────────────────
        if collapsed:
            # Settled: the single event sits on the LAST payment date (the
            # 1st for a pure-prepaid month with no payments this month).
            if day_groups:
                status_event_date = max(
                    date.fromisoformat(day_groups[-1]["date"]), due_date)
            else:
                status_event_date = due_date
            first_pay    = None
            event_status = status          # classify_status → Paid / Prepaid
        elif frozen:
            # Post-settle charge: hands off the settled event entirely.  The
            # anchor derives from the settled snapshot (== the stored anchor
            # on steady frozen runs, so date_changed stays False; after a
            # reactivated cycle whose fresh rows vanished it re-derives the
            # LAST SETTLED payment date the settled body belongs on).
            srows = collapse["settled_rows"]
            if srows:
                status_event_date = max(
                    date.fromisoformat(srows[-1]["date"]), due_date)
            else:
                status_event_date = due_date
            first_pay    = None
            event_status = status
        elif render_groups:
            status_event_date = date.fromisoformat(render_groups[0]["date"])
            # Safety clamp: never place the event before this month's due date
            status_event_date = max(status_event_date, due_date)
            first_pay         = render_groups[0]
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
            len(render_groups) > (
                len(prior.get("payment_event_ids", [])) +
                (1 if (prior_status_id
                       and prior_status_date != due_date.isoformat()) else 0)
            )
            if prior else bool(render_groups)
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

        def _status_body() -> dict:
            """The month's status-event body for the current collapse state.
            Every build/recovery path below goes through here so a settled
            month can never be resurrected in its expanded form."""
            if collapsed:
                return self.gcal._build_settled_month_event(
                    unit, status_event_date, day_groups,
                    promise_history=projected_history,
                    reversal_notes=reversal_notes,
                    settled_on=settled_on)
            if frozen:
                # Rebuild the settled snapshot, not the live balance — the
                # post-settle charge stays invisible on this month.
                stored = collapse["settled_rows"]
                frozen_unit = {
                    **unit,
                    "past_due":    float(settled_past_due or 0.0),
                    "amount_paid": sum(r["amount"] for r in stored
                                       if not r.get("is_nsf")),
                }
                return self.gcal._build_settled_month_event(
                    frozen_unit, status_event_date,
                    group_payments_by_day(stored),
                    promise_history=projected_history,
                    reversal_notes=reversal_notes,
                    settled_on=settled_on)
            return self.gcal._build_status_event(
                unit, event_status, status_event_date,
                first_pay,
                balances[0] if balances else None,
                total_payments=len(render_groups),
                reversal_notes=reversal_notes,
                promise_history=projected_history,
                settled_prefix=settled_prefix,
            )

        # ── Build / update status event ───────────────────────────────────────
        if suppress_kickstart:
            # Commitment anchors this month; no status event on the 1st
            status_event_id = None
            log.info(f"  {oid}: status event suppressed (commitment anchors {this_month})")
        elif frozen and not FORCE_REFRESH:
            # Post-settle charge and no new payment: the settled event is
            # untouchable (the charge surfaces in next month's due amount).
            # Two exceptions: the event vanished from the calendar (self-
            # heal), or the month just left a REACTIVATED cycle whose fresh
            # rows all vanished — the live event still shows the stale fresh
            # tracking, so restore the settled body in place.
            status_event_id = prior_status_id
            left_reactivated = bool(
                prior and prior.get("collapse_state") == "reactivated")
            if left_reactivated and status_event_id:
                status_event_id = self.gcal._update_or_create(
                    calendar_id, status_event_id, _status_body())
                log.info(
                    f"  {oid}: fresh-cycle rows vanished — restored the "
                    f"settled event body on {status_event_date}")
            elif not (status_event_id
                      and self.gcal.get_event(calendar_id, status_event_id)):
                existing_id = self.gcal._find_status_event(
                    calendar_id, oid, this_month)
                status_event_id = self.gcal._update_or_create(
                    calendar_id, existing_id, _status_body())
                log.warning(
                    f"  {oid}: frozen settled event was missing — recreated "
                    f"from stored records on {status_event_date}")
            else:
                log.info(f"  {oid}: month frozen (charge after settlement) — "
                         f"settled event left untouched")
        elif (FORCE_REFRESH or date_changed or data_changed
                or collapse["transitioned"] or fmt_migrate):
            body = _status_body()
            existing_id = (
                prior_status_id or
                self.gcal._find_status_event(calendar_id, oid, this_month)
            )
            status_event_id = self.gcal._update_or_create(
                calendar_id, existing_id, body)
            log.info(f"  Status event {oid}: "
                     f"{'settled (collapsed)' if collapsed else event_status} "
                     f"on {status_event_date}")
        elif prior_status_id is None:
            # RECOVERY: a prior run suppressed this event (commitment was active)
            # but the commitment has since been removed or resolved.  The state
            # has status_event_id=None and rent_event_id=None, data hasn't changed,
            # so the normal paths all skip.  Force-create the missing event.
            body = _status_body()
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
                body = _status_body()
                # Search the calendar first to avoid creating duplicates
                existing_id = self.gcal._find_status_event(
                    calendar_id, oid, this_month)
                status_event_id = self.gcal._update_or_create(
                    calendar_id, existing_id, body)
            else:
                log.info(f"  No change for {oid} — skipping status event")

        # ── Additional payment events (one per day-group; none while settled) ─
        if frozen:
            # A settled month keeps no payment events; any leftovers (e.g. a
            # reactivated cycle whose rows vanished) become surplus for the
            # cleanup below — never carry their ids forward.
            payment_event_ids = []
        elif (FORCE_REFRESH or data_changed or new_payments
                or collapse["transitioned"] or fmt_migrate):
            payment_event_ids = self._sync_additional_payments(
                unit, calendar_id, this_month,
                render_groups[1:], balances[1:], prior,
            )
        else:
            payment_event_ids = prior.get("payment_event_ids", []) if prior else []

        # ── Detect-and-revert locked events ──────────────────────────────────
        if not suppress_kickstart and not (FORCE_REFRESH or date_changed or data_changed):
            self._verify_locked_events(
                soid, calendar_id, prior,
                status_event_id, status_event_date, render_groups,
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
            # The pre-listed snapshot + this run's fresh conversions — the
            # same merge submit mode does, so a promise converted after the
            # listing is never mistaken for PM-deleted.
            events=self._commitment_events_for(soid, live_commitments),
            payment_dates=promise_payment_dates,
        )

        # ── Persist current-month state ───────────────────────────────────────
        self.state.set(soid, this_month, {
            "fmt":               STATE_FMT,
            "status":            status,
            "past_due":          past_due,
            "calendar_id":       calendar_id,
            "status_event_id":   status_event_id,
            "status_event_date": status_event_date.isoformat(),
            "late_event_id":     late_event_id,
            "payment_event_ids": payment_event_ids,
            # Index-aligned with payment_event_ids — one entry per DAY-GROUP
            # (same-day payments render as a single event).  Submit mode has
            # no ledger, so payment-drag detection reads canonical dates here.
            "payment_event_dates": [g["date"] for g in render_groups[1:]],
            # All of this month's payments, one entry per LEDGER ROW (never
            # grouped) — NSF-reversal reconciliation and the settled-baseline
            # integrity checks match against these without any event fetches.
            "payments": [
                {"date": p["date"], "amount": p["amount"],
                 "is_nsf": p["is_nsf"], "description": p["description"]}
                for p in sorted_payments
            ],
            # Settled-month collapse bookkeeping (transforms.
            # resolve_collapse_transition owns the transitions).
            "collapse_state":    collapse["state"],
            "collapse_baseline": len(collapse["settled_rows"]),
            "settled_rows": [
                {"date": p["date"], "amount": p["amount"],
                 "is_nsf": p["is_nsf"], "description": p["description"]}
                for p in collapse["settled_rows"]
            ],
            "settled_past_due":  settled_past_due,
            "settled_on":        settled_on,
            # Kept/resolved promises (rendered as "Promise history" in the
            # settled event): carried records + this run's outcomes.
            "promise_history":   self._merge_promise_history(
                carried_promise_history,
                self._pending_promise_history.pop((soid, this_month), [])),
            "nsf_reversals_applied": carried_reversals,
            "nsf_event_ids":         carried_nsf_ids,
        })

        # ── NSF reversal reconciliation (current + previous 2 months) ────────
        # Reversals arrive as negative-credit ledger rows; the affected
        # month may already be rolled over.  Runs after the current-month
        # rebuild (so flips/notes are not overwritten) and never touches the
        # future-months section below.
        if reversal_map:
            try:
                self._apply_nsf_reversals(
                    soid, calendar_id, unit, today, this_month, reversal_map,
                    surplus_payment_ids=prior_payment_ids[len(payment_event_ids):],
                    prior_payments=prior_payments,
                )
            except Exception as exc:
                log.error(f"  {oid}: NSF reversal pass failed: {exc}",
                          exc_info=True)

        # ── Surplus payment-event cleanup ─────────────────────────────────────
        # Deletes every current-month payment event that should no longer
        # exist: ALL of them when the month is settled (their history moved
        # into the settled event's description), plus orphaned strays whose
        # ledger rows vanished without a reversal record — a long-standing
        # leak (the old cleanup only ran inside the reversal pass).  Runs
        # after the reversal pass so flips/ghosts are tracked first.
        try:
            self._cleanup_surplus_payment_events(
                soid, calendar_id, oid, this_month,
                keep_ids=set(payment_event_ids),
                prior_payment_ids=prior_payment_ids,
                collapsed=(collapsed or frozen),
                live_scan=(deep_clean or fmt_migrate
                           or collapse["transitioned"]),
            )
        except Exception as exc:
            log.error(f"  {oid}: payment-event cleanup failed: {exc}",
                      exc_info=True)

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
            # (Submit mode mirrors this in _absorb_promised_placeholders —
            # keep the two in sync.)
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
                # fmt stamped here so the entry doesn't read as pre-collapse
                # when the month turns current (that would force a needless
                # deep-clean scan every month rollover).
                "fmt":           STATE_FMT,
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
    ) -> list[str]:
        """Sync payment events for day-groups idx 1+ (group 0 is absorbed
        into the status event).  One event per group, its id positionally
        reused run-to-run; a settled month passes an empty list."""
        prior_ids  = prior.get("payment_event_ids", []) if prior else []
        month_recv = unit["amount_paid"]
        total      = len(additional) + 1
        event_ids  = []

        for i, (payment, balance) in enumerate(zip(additional, balances)):
            body = self.gcal._build_additional_payment_event(
                unit, payment, i + 2, total, balance, month_recv)
            existing = prior_ids[i] if i < len(prior_ids) else None
            if not existing:
                existing = self.gcal._find_payment_event(
                    calendar_id, unit["occupancy_id"], this_month, i + 1)
            eid = self.gcal._update_or_create(calendar_id, existing, body)
            event_ids.append(eid)
            log.info(
                f"  Payment group {i+2}/{total} for {unit['occupancy_id']} "
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
        render_groups: list[dict],
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
        drag-detection primitives above.)  render_groups = this run's
        day-groups (canonical payment-event dates come from groups[1:]).
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
            [g["date"] for g in render_groups[1:]],
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
        payment_dates: Optional[set] = None,
    ):
        """
        For each tracked commitment:
          1. Discover new copies (PM copy-pasted for split payment plans).
          2. Absorb: ANY (non-NSF) payment dated on a promise's live anchor
             deletes that one promise — the tenant kept the date, whatever
             the amount.  Recorded as outcome "kept"; other promises stay.
             Never kickstarts.  payment_dates=None (submit mode: no ledger)
             skips this — the next full sweep absorbs.
          3. Resolve (delete every promise) if account balance ≤ 0; promise-
             typed sources are recorded as outcome "resolved".
          4. Update the auto section, preserving PM notes above the divider.
             Display recomputes from the live balance: 🔴 when nothing has been
             paid this month, 🟡 when a partial payment leaves a balance.  A
             promise whose date has already passed is NOT specially flagged — it
             keeps its 🔴 / 🟡 colour (no auto-expire, no ⚠️ overdue state).
             Also picks up re-drags (PM moved the commitment again).
          5. Safe delete (≥1-promise rule): a deleted promise sticks only while
             another promise remains; deleting the LAST promise recreates one.
             An ABSORBED promise never counts as deleted: its entry lands in
             neither `surviving` nor `missing_promises`, so a still-owing unit
             may legitimately end up with zero promises after keeping its
             last promised date.
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

            # ── Absorb: the tenant paid on the promised date ─────────────────
            # Checked before resolution so a same-day settling payment reads
            # "kept", not merely "resolved".  Uses the LIVE anchor (a re-drag
            # counts); falls back to the registry anchor when the event is
            # already gone — a PM deleting a kept promise must not trip the
            # ≥1-promise rule either.  A promise created/converted THIS run
            # is exempt: the PM just placed it deliberately, so it survives
            # at least one full poll before a same-day payment can absorb it.
            if (payment_dates is not None and _is_promise(source_type)
                    and event_id not in self._fresh_commitments):
                probe = live_by_id.get(event_id)
                live_anchor = anchor_date
                if probe:
                    start = probe.get("start", {})
                    live_anchor = (start.get("date")
                                   or start.get("dateTime", "")[:10]
                                   or anchor_date)
                if live_anchor in payment_dates:
                    if probe is not None:
                        self.gcal.delete_event(calendar_id, event_id)
                    self._record_promise_outcome(
                        soid, today_month, {**c, "anchor_date": live_anchor},
                        "kept", today)
                    log.info(
                        f"  {bare_oid}: promise for {live_anchor} absorbed — "
                        f"payment received that day (any amount keeps the "
                        f"promised date)")
                    continue

            # ── Resolve if fully paid ─────────────────────────────────────────
            if past_due <= 0:
                if _is_promise(source_type):
                    self._record_promise_outcome(
                        soid, today_month, c, "resolved", today)
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

    # ── Promise history (kept / resolved promises) ────────────────────────────

    @staticmethod
    def _merge_promise_history(base: list, extra: list) -> list:
        """Merge promise-history records, deduplicated by (event_id, outcome)
        — a re-run must never double-log an outcome."""
        merged = list(base)
        seen = {(r.get("event_id"), r.get("outcome")) for r in merged}
        for r in extra:
            key = (r.get("event_id"), r.get("outcome"))
            if key not in seen:
                merged.append(r)
                seen.add(key)
        return merged

    def _record_promise_outcome(self, soid: str, month: str,
                                commitment: dict, outcome: str, today: date):
        """Queue a kept/resolved promise record; _sync_unit's state rewrite
        (or the run-tail flush) persists it into the month entry."""
        self._pending_promise_history.setdefault((soid, month), []).append({
            "event_id":          commitment.get("event_id"),
            "anchor_date":       commitment.get("anchor_date"),
            "source_type":       commitment.get("source_type") or "late",
            "origin_month":      commitment.get("origin_month"),
            "covers_rent_month": commitment.get("covers_rent_month"),
            "outcome":           outcome,
            "recorded":          today.isoformat(),
        })

    def _project_promise_outcomes(self, commitments: list, calendar_id: str,
                                  payment_dates: set, past_due: float,
                                  live_anchor_by_id: Optional[dict] = None,
                                  ) -> list:
        """
        Stateless preview of what this run's commitment pass will record —
        the status event is built BEFORE _process_commitments runs, and a
        settled month must list its promises in the SAME run they resolve.
        live_anchor_by_id (event_id → live start date) keeps the projection
        byte-consistent with the absorption pass, which honours re-drags;
        the registry anchor is only the fallback for already-gone events.
        """
        live_anchor_by_id = live_anchor_by_id or {}
        projected = []
        for c in commitments:
            c_cal = c.get("calendar_id")
            if c_cal and c_cal != calendar_id:
                continue
            src = c.get("source_type") or "late"
            if src not in ("status", "payment", "late"):
                continue
            anchor = (live_anchor_by_id.get(c.get("event_id"))
                      or c.get("anchor_date"))
            rec = {
                "event_id":          c.get("event_id"),
                "anchor_date":       anchor,
                "source_type":       src,
                "origin_month":      c.get("origin_month"),
                "covers_rent_month": c.get("covers_rent_month"),
                "recorded":          None,   # display only; the real record
                                             # is written by the commitment pass
            }
            if anchor in payment_dates:
                projected.append({**rec, "outcome": "kept"})
            elif past_due <= 0:
                projected.append({**rec, "outcome": "resolved"})
        return projected

    def _flush_pending_promise_history(self):
        """Persist queued promise outcomes whose month entry was not
        rewritten this run (submit mode; a unit that failed mid-sync)."""
        for (soid, month), recs in list(self._pending_promise_history.items()):
            if not recs:
                continue
            entry = self.state.get(soid, month)
            if entry is None:
                log.info(
                    f"  {soid}: no {month} entry to persist {len(recs)} "
                    f"promise-history record(s) — dropping")
                continue
            self.state.set(soid, month, {
                **entry,
                "promise_history": self._merge_promise_history(
                    list(entry.get("promise_history") or []), recs),
            })
        self._pending_promise_history = {}

    # ── Surplus payment-event cleanup ──────────────────────────────────────────

    def _cleanup_surplus_payment_events(
        self, soid: str, calendar_id: str, oid: str, this_month: str,
        keep_ids: set, prior_payment_ids: list,
        collapsed: bool, live_scan: bool,
    ):
        """
        Delete current-month payment events that should no longer exist.

        A settled (collapsed/frozen) month keeps NO payment events — the
        settled event's description carries the history, and nsf_event_ids
        are cleared.  An expanded month keeps this run's day-group events
        plus tracked NSF flips/ghosts.  Candidates come from state; the live
        extended-property scan is added only on deep-clean runs (collapse
        transitions, the fmt migration, the nightly) so the hourly sweep's
        API volume stays flat.
        """
        entry   = self.state.get(soid, this_month) or {}
        nsf_ids = [i for i in (entry.get("nsf_event_ids") or []) if i]
        keep    = set() if collapsed else (set(keep_ids) | set(nsf_ids))
        candidates = {i for i in prior_payment_ids if i}
        if collapsed:
            candidates |= set(nsf_ids)
        if live_scan:
            try:
                for ev in self.gcal.find_month_payment_events(
                        calendar_id, oid, this_month):
                    candidates.add(ev["id"])
            except HttpError as e:
                log.error(f"  {oid}: payment-event scan failed: {e}")
        doomed = sorted(candidates - keep)
        for eid in doomed:
            try:
                self.gcal.delete_event(calendar_id, eid)
                log.info(
                    f"  {oid}: removed surplus payment event {eid}"
                    + (" (month settled — consolidated)" if collapsed else ""))
            except HttpError as e:
                log.error(f"  {oid}: failed to delete payment event {eid}: {e}")
        if collapsed and nsf_ids:
            entry = self.state.get(soid, this_month) or {}
            self.state.set(soid, this_month, {**entry, "nsf_event_ids": []})

    # ── NSF reversal reconciliation ───────────────────────────────────────────

    @staticmethod
    def _format_reversal_note(rec: dict) -> str:
        try:
            d = date.fromisoformat(rec.get("date") or "").strftime("%b %d, %Y")
        except ValueError:
            d = rec.get("date") or "?"
        amt = float(rec.get("amount") or 0)
        return f"⚠️ ${amt:,.2f} payment REVERSED (NSF) on {d}"

    @staticmethod
    def _reversal_matches_row(rec: dict, row: dict) -> bool:
        """Does this reversal refer to this LEDGER ROW?  Ref-token match on
        the row's description when the reversal carries one; else an exact
        amount match (½¢ tolerance)."""
        ref = rec.get("ref")
        if ref:
            return re.search(rf"#{re.escape(ref)}(?![\w-])",
                             row.get("description") or "") is not None
        return abs(float(row.get("amount") or 0)
                   - float(rec.get("amount") or 0)) < 0.005

    @staticmethod
    def _reversal_matches_text(rec: dict, text: str) -> bool:
        """Does this reversal refer to the payment described by text (an
        event body)?  Ref-token match (boundary-guarded so #1A4A can never
        match #1A4A-5A70) when the reversal carries one; else an exact
        Amount-line match."""
        ref = rec.get("ref")
        if ref:
            return re.search(rf"#{re.escape(ref)}(?![\w-])", text) is not None
        amt = float(rec.get("amount") or 0)
        return re.search(
            rf"^Amount:\s+\${re.escape(f'{amt:,.2f}')}$", text, re.M) is not None

    def _apply_nsf_reversals(
        self, soid: str, calendar_id: str, unit: dict, today: date,
        this_month: str, reversal_map: dict,
        surplus_payment_ids: list, prior_payments: list,
    ):
        """
        Attribute negative-credit reversal rows (NSF bounces) to this unit's
        events and redraw the affected month — current or one of the previous
        two.  Runs per soid: each co-owner calendar flips its OWN event
        copies, and the idempotence markers (nsf_reversals_applied) live in
        that soid's month entries, so every reversal is applied exactly once
        per calendar.  Unmatched reversals warn and retry each run until a
        62-day age-out (the ledger's ~current-month window usually ages them
        out sooner).  Dollar lines inside prior-month events are left as
        written (they were true at the time); the appended note carries the
        correction.
        """
        # Tenant match, exactly like _make_unit's payment lookup.
        records = reversal_map.get(normalize_tenant_name(unit["tenant"]), [])
        if not records:
            for name in (unit.get("additional_tenants") or "").split(","):
                name_norm = normalize_tenant_name(name.strip())
                if name_norm and name_norm in reversal_map:
                    records = reversal_map[name_norm]
                    break
        if not records:
            return

        oid = soid.split("@")[0] if "@" in soid else soid

        # Months searched: current + previous two.
        months = [this_month]
        y, m = int(this_month[:4]), int(this_month[5:7])
        for _ in range(2):
            m -= 1
            if m == 0:
                y, m = y - 1, 12
            months.append(f"{y:04d}-{m:02d}")

        def _key(rec):
            return rec.get("ref") or f"{rec.get('date')}:{float(rec.get('amount') or 0):.2f}"

        # Markers are versioned: v=2 wrote the full flip (Status line
        # rewritten, balance lines stamped).  Older markers are treated as
        # pending once more so events flipped by earlier code get a single
        # self-healing retouch — every mutation on that path is idempotent,
        # then the marker upgrades and the reversal is skipped for good.
        applied_keys = set()
        for mo in months:
            entry = self.state.get(soid, mo)
            if entry and entry.get("calendar_id") == calendar_id:
                for r in entry.get("nsf_reversals_applied") or []:
                    if r.get("v") == 2:
                        applied_keys.add(r.get("key"))

        def _mark(month, rec, extra_event_id=None):
            entry = self.state.get(soid, month) or {}
            marks = [m for m in (entry.get("nsf_reversals_applied") or [])
                     if m.get("key") != _key(rec)]
            marks.append({"key": _key(rec), "ref": rec.get("ref"),
                          "date": rec.get("date"), "amount": rec.get("amount"),
                          "v": 2})
            new_entry = {**entry, "nsf_reversals_applied": marks}
            if extra_event_id:
                ids = list(entry.get("nsf_event_ids") or [])
                if extra_event_id not in ids:
                    ids.append(extra_event_id)
                new_entry["nsf_event_ids"] = ids
            self.state.set(soid, month, new_entry)
            applied_keys.add(_key(rec))

        pending = []
        for rec in records:
            try:
                too_old = (date.fromisoformat(rec.get("date") or "")
                           < today - timedelta(days=62))
            except ValueError:
                log.debug(f"  {oid}: reversal with bad date "
                          f"{rec.get('date')!r} — ignoring")
                continue
            if not too_old and _key(rec) not in applied_keys:
                pending.append(rec)
        if not pending:
            return

        # ── Current month: flip/delete surplus events; note vanished rows ──
        cur_entry = self.state.get(soid, this_month) or {}
        month_settled = cur_entry.get("collapse_state") in ("collapsed",
                                                            "frozen")
        for ev_id in surplus_payment_ids:
            body = self.gcal.get_event(calendar_id, ev_id)
            if not body:
                continue
            desc = body.get("description") or ""
            matched = next((r for r in pending
                            if self._reversal_matches_text(r, desc)), None)
            if matched and month_settled:
                # A settled month keeps no payment events (the cleanup would
                # delete a flipped marker moments later) — record the bounce
                # on the settled event instead, where it survives: patched
                # notes persist because settled months never rebuild, and
                # any forced rebuild re-renders them from the v2 marker.
                note = self._format_reversal_note(matched)
                sid = cur_entry.get("status_event_id")
                if sid:
                    sbody = self.gcal.get_event(calendar_id, sid)
                    if sbody:
                        self.gcal.append_description_note(
                            calendar_id, sbody, note)
                self.gcal.delete_event(calendar_id, ev_id)
                _mark(this_month, matched)
                pending.remove(matched)
                log.info(f"  {oid}: reversed payment noted on the settled "
                         f"event; surplus marker {ev_id} removed")
            elif matched:
                self.gcal.flip_event_to_nsf(
                    calendar_id, body, self._format_reversal_note(matched),
                    retag_idx=True)
                _mark(this_month, matched, extra_event_id=ev_id)
                pending.remove(matched)
                log.info(f"  {oid}: flipped reversed payment event {ev_id} to NSF")
            else:
                # A vanished ledger row with no matching reversal left a
                # positionally-duplicated stale event — remove it (the next
                # sweep recreates it if the row ever returns).
                self.gcal.delete_event(calendar_id, ev_id)
                log.warning(
                    f"  {oid}: deleted stale surplus payment event {ev_id} "
                    f"(ledger row vanished, no reversal match)")

        still = []
        for rec in pending:
            hit = None
            ref = rec.get("ref")
            if ref:
                for p in prior_payments:
                    if re.search(rf"#{re.escape(ref)}(?![\w-])",
                                 p.get("description") or ""):
                        hit = p
                        break
            else:
                amt_matches = [
                    p for p in prior_payments
                    if not p.get("is_nsf")
                    and abs(float(p.get("amount") or 0)
                            - float(rec.get("amount") or 0)) < 0.005]
                if len(amt_matches) == 1:
                    hit = amt_matches[0]
            if hit is not None:
                # The month's figures were already rebuilt truthfully (the
                # past_due jump fired data_changed); PATCH the note on now —
                # future rebuilds re-render it from the carried marker.
                note     = self._format_reversal_note(rec)
                ghost_id = None
                row_vanished = not any(
                    self._reversal_matches_row(rec, p)
                    for p in (unit.get("payments") or []))
                if row_vanished and not month_settled:
                    # The bounced payment's positive row vanished with it, so
                    # no event remains to flip — reconstruct a red NSF event
                    # from the stored row (this is the "NSF payment in red"
                    # after a settled-collapse revert).  Skipped while the
                    # month is still settled (the settled event's description
                    # carries the note instead) and when the positive row
                    # SURVIVED the pull keyword-flagged NSF — its natural red
                    # rendering already shows the bounce; a ghost would
                    # display it twice.
                    try:
                        gbody = self.gcal._build_nsf_ghost_event(
                            unit, hit, note, this_month)
                        created = _gcal_execute(
                            self.gcal.service.events().insert(
                                calendarId=calendar_id, body=gbody))
                        ghost_id = created["id"]
                        log.info(f"  {oid}: reconstructed red NSF event for "
                                 f"vanished payment ({_key(rec)})")
                    except HttpError as e:
                        log.error(
                            f"  {oid}: failed to create NSF ghost event: {e}")
                sid = cur_entry.get("status_event_id")
                if sid:
                    body = self.gcal.get_event(calendar_id, sid)
                    if body:
                        self.gcal.append_description_note(
                            calendar_id, body, note)
                _mark(this_month, rec, extra_event_id=ghost_id)
                log.info(f"  {oid}: recorded NSF reversal of a vanished "
                         f"current-month payment ({_key(rec)})")
            else:
                still.append(rec)
        pending = still

        # ── Previous months ────────────────────────────────────────────────
        for mo in months[1:]:
            if not pending:
                break
            entry = self.state.get(soid, mo)
            if not entry or entry.get("calendar_id") != calendar_id:
                continue
            # A settled or reactivated prior month can't take the legacy
            # flip path: settled months have no payment events, and both
            # settled and reactivated status events embed the settled rows'
            # Amount lines — a text match would flip the whole month's
            # status event red.  Un-collapse first (expanded events rebuilt
            # from the stored rows, bounced row red); any remaining
            # reversals then match the rebuilt events below.
            if entry.get("collapse_state") in ("collapsed", "frozen",
                                               "reactivated"):
                pending = self._uncollapse_prior_month(
                    soid, calendar_id, unit, mo, entry, pending, _mark, today)
                if not pending:
                    break
                entry = self.state.get(soid, mo)
                if (not entry
                        or entry.get("collapse_state") in ("collapsed",
                                                           "frozen",
                                                           "reactivated")):
                    continue
            candidates = []
            if entry.get("status_event_id"):
                candidates.append(entry["status_event_id"])
            candidates.extend(entry.get("payment_event_ids") or [])
            if not candidates:
                continue
            bodies = {}
            for ev_id in candidates:
                b = self.gcal.get_event(calendar_id, ev_id)
                if b:
                    bodies[ev_id] = b
            still = []
            for rec in pending:
                matches = [ev_id for ev_id, b in bodies.items()
                           if self._reversal_matches_text(
                               rec, b.get("description") or "")]
                if rec.get("ref") is None and len(matches) > 1:
                    log.warning(
                        f"  {oid}: reversal ({_key(rec)}) matches multiple "
                        f"events in {mo} by amount — ambiguous, skipping")
                    still.append(rec)
                    continue
                if not matches:
                    still.append(rec)
                    continue
                ev_id = matches[0]
                note  = self._format_reversal_note(rec)
                self.gcal.flip_event_to_nsf(calendar_id, bodies[ev_id], note)
                log.info(f"  {oid}: flipped {mo} event {ev_id} to NSF (reversal)")
                # The month is no longer settled — un-grey its muted events.
                for other_id, ob in bodies.items():
                    if other_id != ev_id:
                        self.gcal.unmute_event_to_own_status(calendar_id, ob)
                # Explain on the month's status event when it wasn't the one.
                sid = entry.get("status_event_id")
                if sid and sid != ev_id and sid in bodies:
                    self.gcal.append_description_note(
                        calendar_id, bodies[sid],
                        note + " — month no longer settled")
                _mark(mo, rec)
            pending = still

        for rec in pending:
            log.warning(
                f"  {oid}: NSF reversal ({_key(rec)}) has no matching payment "
                f"event in {months} — will retry until it ages out")

    def _uncollapse_prior_month(
        self, soid: str, calendar_id: str, unit: dict, mo: str,
        entry: dict, pending: list, mark, today: date,
    ) -> list:
        """
        A reversal arrived for a month that COLLAPSED (or froze, or rolled
        over mid-REACTIVATION) — the settlement was fiction.  Rebuild the
        month's expanded view from the stored ledger rows: status event back
        on the first payment date (reusing the settled event's id in place,
        so nothing orphans), one event per remaining day-group, the bounced
        row red.  Post-reversal balances are honestly reconstructable as
        stored past_due + the reversed amount (a frozen/reactivated month's
        stored past_due already includes its post-settle charge).  Returns
        the still-pending reversals — an unmatched or ambiguous one leaves
        the month settled and retries until age-out.
        """
        oid  = soid.split("@")[0] if "@" in soid else soid
        rows = [dict(r) for r in (entry.get("payments") or [])]
        matched_rec = matched_row = None
        for rec in pending:
            ref = rec.get("ref")
            if ref:
                for r in rows:
                    if not r.get("is_nsf") and re.search(
                            rf"#{re.escape(ref)}(?![\w-])",
                            r.get("description") or ""):
                        matched_rec, matched_row = rec, r
                        break
            else:
                amt_matches = [
                    r for r in rows
                    if not r.get("is_nsf")
                    and abs(float(r.get("amount") or 0)
                            - float(rec.get("amount") or 0)) < 0.005]
                if len(amt_matches) == 1:
                    matched_rec, matched_row = rec, amt_matches[0]
                elif len(amt_matches) > 1:
                    log.warning(
                        f"  {oid}: reversal ({rec.get('date')}:"
                        f"{rec.get('amount')}) matches multiple stored "
                        f"payments in settled {mo} — ambiguous, skipping")
            if matched_rec:
                break
        if not matched_rec:
            return pending

        matched_row["is_nsf"] = True
        note = (self._format_reversal_note(matched_rec)
                + " — month no longer settled; events reconstructed from "
                  "sync records (balances shown are post-reversal)")
        final_pd = (float(entry.get("past_due") or 0)
                    + float(matched_rec.get("amount") or 0))
        rows.sort(key=lambda p: (p.get("date") or "",
                                 -float(p.get("amount") or 0)))
        groups   = group_payments_by_day(rows)
        balances = compute_running_balances(groups, final_pd)
        try:
            mo_due = date(int(mo[:4]), int(mo[5:7]), RENT_DUE_DAY)
        except (ValueError, IndexError):
            mo_due = today.replace(day=RENT_DUE_DAY)
        month_recv = sum(r["amount"] for r in rows if not r.get("is_nsf"))
        unit_mo = {**unit, "past_due": final_pd, "amount_paid": month_recv,
                   "payments": rows}

        if groups:
            anchor    = max(date.fromisoformat(groups[0]["date"]), mo_due)
            first     = groups[0]
            ev_status = payment_status(unit["rent"], balances[0])
        else:
            anchor, first = mo_due, None
            ev_status     = classify_status(unit["rent"], final_pd)
        body = self.gcal._build_status_event(
            unit_mo, ev_status, anchor, first,
            balances[0] if balances else None,
            total_payments=len(groups),
            reversal_notes=[note],
            promise_history=list(entry.get("promise_history") or []),
        )
        sid = self.gcal._update_or_create(
            calendar_id, entry.get("status_event_id"), body)

        new_ids = []
        for i, (g, bal) in enumerate(zip(groups[1:], balances[1:])):
            pbody = self.gcal._build_additional_payment_event(
                unit_mo, g, i + 2, len(groups), bal, month_recv)
            existing = self.gcal._find_payment_event(
                calendar_id, oid, mo, i + 1)
            new_ids.append(self.gcal._update_or_create(
                calendar_id, existing, pbody))

        self.state.set(soid, mo, {
            **entry,
            "fmt":               STATE_FMT,
            "status":            ev_status,
            "past_due":          final_pd,
            "status_event_id":   sid,
            "status_event_date": anchor.isoformat(),
            "payment_event_ids": new_ids,
            "payment_event_dates": [g["date"] for g in groups[1:]],
            "payments":          rows,
            "collapse_state":    None,
            "collapse_baseline": 0,
            "settled_rows":      [],
            "settled_past_due":  None,
            "settled_on":        None,
        })
        mark(mo, matched_rec)
        log.info(
            f"  {oid}: reversal broke {mo}'s settlement — un-collapsed "
            f"({len(groups)} day-group(s) rebuilt, bounced payment red)")
        return [r for r in pending if r is not matched_rec]
