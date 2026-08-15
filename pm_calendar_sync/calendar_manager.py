"""Google Calendar manager: calendar/event CRUD and event builders."""
import json
import re
import time
from datetime import date, timedelta
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    GOOGLE_SA_JSON, GOOGLE_SCOPES, PM_EMAIL, CALENDAR_PREFIX, RETIRED_PREFIX,
    RENT_DUE_DAY, LATE_GRACE_DAYS, COMMITMENT_DIVIDER,
    COLOR_PARTIAL, COLOR_UNPAID, COLOR_SETTLED,
    GCAL_RETRY_ATTEMPTS, GCAL_RETRY_BASE_DELAY, log,
)
from .status import (
    classify_status, payment_status, color_for_status, emoji_for_status,
    STATUS_PAID, STATUS_PREPAID, STATUS_PARTIAL, STATUS_UNPAID,
    STATUS_LATE,
)
from .transforms import normalize_tenant_name, parse_status_line, _next_day


def _gcal_execute(request, retries: int = GCAL_RETRY_ATTEMPTS,
                  base_delay: int = GCAL_RETRY_BASE_DELAY):
    """
    Execute a Google Calendar API request with exponential backoff on
    403 (per-user rate limit) / 429 (rate limit) / 500 / 503 (server errors).
    """
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in (403, 429, 500, 503) and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(
                    f"  Google API {e.resp.status} — "
                    f"retry {attempt + 1}/{retries} in {delay}s")
                time.sleep(delay)
            else:
                raise


_PROMISE_SOURCE_LABEL = {
    "status":    "Status",
    "payment":   "Payment",
    "late":      "Late",
    "kickstart": "Kickstart",
}


def _promise_history_lines(promise_history: list) -> list[str]:
    """Render persisted promise-history records (see StateManager docstring)
    as description lines for the settled/status event."""
    lines = ["Promise history:"]
    for rec in promise_history:
        anchor = rec.get("anchor_date") or "?"
        try:
            anchor = date.fromisoformat(anchor).strftime("%b %d, %Y")
        except ValueError:
            pass
        src = _PROMISE_SOURCE_LABEL.get(
            rec.get("source_type") or "",
            (rec.get("source_type") or "?").title())
        verdict = ("KEPT (payment received that day)"
                   if rec.get("outcome") == "kept"
                   else "RESOLVED (balance cleared)")
        lines.append(f"• {src} promise for {anchor} — {verdict}")
    return lines


def _payment_rows(payment_or_group: dict) -> list[dict]:
    """Member ledger rows of a day-group; a bare payment is its own row."""
    return payment_or_group.get("rows") or [payment_or_group]


class GoogleCalendarManager:

    def __init__(self):
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_SA_JSON), scopes=GOOGLE_SCOPES)
        self.service = build("calendar", "v3", credentials=creds)
        self._cal_cache: dict = {}
        # Calendar ids inserted during THIS process — the orchestrator uses
        # this to run ACL sharing for brand-new calendars even in run modes
        # that otherwise skip the per-owner ACL refresh.
        self.created_calendar_ids: set = set()

    # ── Calendar management ──────────────────────────────────────────────────

    def get_or_create_group_calendar(self, group_name: str) -> str:
        """Resolve (or create) the calendar for a property group.

        The summary is the AppFolio group name VERBATIM — deliberately not
        "... Portfolio", so a group named after an owner can never adopt that
        owner's retired legacy calendar by summary match.
        """
        cache_key = f"group:{group_name}"
        if cache_key in self._cal_cache:
            return self._cal_cache[cache_key]
        page_token = None
        found_id = None
        while True:
            resp = _gcal_execute(
                self.service.calendarList().list(pageToken=page_token))
            for cal in resp.get("items", []):
                if cal["summary"] == group_name:
                    found_id = cal["id"]
                    break
            if found_id:
                break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if found_id:
            self._cal_cache[cache_key] = found_id
            return found_id
        cal = _gcal_execute(self.service.calendars().insert(body={
            "summary": group_name,
            "description": (f"Rent tracking for the {group_name} property "
                            f"group. Do not edit — auto-synced from AppFolio."),
            "timeZone": "America/Chicago",
        }))
        log.info(f"Created group calendar: {group_name}")
        self.created_calendar_ids.add(cal["id"])
        # PM gets owner so the calendar appears under 'My calendars'
        if PM_EMAIL:
            self._share(cal["id"], PM_EMAIL, role="owner", notify=False)
        self._cal_cache[cache_key] = cal["id"]
        return cal["id"]

    def ensure_calendar_summary(self, calendar_id: str, summary: str) -> bool:
        """Verify a calendar still exists (by id) and that its summary matches;
        patch the summary in place when it drifted (e.g. the property group was
        renamed in AppFolio).  Returns False when the calendar is gone —
        resolve-by-id healing beats the old resolve-by-name, which would have
        silently created an orphan duplicate on any rename."""
        try:
            cal = _gcal_execute(self.service.calendars().get(
                calendarId=calendar_id))
        except HttpError as e:
            if e.resp.status in (404, 410):
                return False
            raise
        if cal.get("summary") != summary:
            _gcal_execute(self.service.calendars().patch(
                calendarId=calendar_id, body={"summary": summary}))
            log.info(f"Renamed calendar: {cal.get('summary')!r} → {summary!r}")
        return True

    def retire_calendar(self, calendar_id: str) -> bool:
        """Idempotently retire a legacy owner calendar: prefix its summary
        with RETIRED_PREFIX and revoke every user ACL except the PM and the
        service account.  History (events, PM notes) is kept.  Never raises —
        a single stubborn calendar must not abort the cutover; returns False
        so the caller can log and let the next full run retry."""
        try:
            try:
                cal = _gcal_execute(self.service.calendars().get(
                    calendarId=calendar_id))
            except HttpError as e:
                if e.resp.status in (404, 410):
                    log.warning(f"  Calendar {calendar_id} already deleted — "
                                f"nothing to retire")
                    return True
                raise
            summary = cal.get("summary", "")
            if not summary.startswith(RETIRED_PREFIX):
                _gcal_execute(self.service.calendars().patch(
                    calendarId=calendar_id,
                    body={"summary": f"{RETIRED_PREFIX}{summary}"}))
                log.info(f"  Retired calendar: {summary!r} → "
                         f"{RETIRED_PREFIX}{summary!r}")
            acl = _gcal_execute(self.service.acl().list(calendarId=calendar_id))
            for rule in acl.get("items", []):
                scope = rule.get("scope", {})
                if scope.get("type") != "user":
                    continue  # never touch default/domain rules
                value = (scope.get("value") or "").lower()
                if value == PM_EMAIL.lower() or "gserviceaccount.com" in value:
                    continue
                if value == calendar_id.lower():
                    # The calendar's own primary-owner pseudo-rule (scope
                    # value == calendar id).  Google forbids touching it —
                    # deleting returns 403 cannotChangeOwnerAcl.
                    continue
                try:
                    _gcal_execute(self.service.acl().delete(
                        calendarId=calendar_id, ruleId=rule["id"]))
                except HttpError as e:
                    if e.resp.status == 403:
                        # Undeletable rule (e.g. another owner-level ACL) —
                        # leave it; the [RETIRED] rename is what matters.
                        log.warning(f"  Could not revoke "
                                    f"{scope.get('value')}: {e}")
                        continue
                    raise
                log.info(f"  Revoked access for {scope.get('value')}")
            return True
        except Exception as e:
            log.error(f"  Could not retire calendar {calendar_id}: {e}")
            return False

    def get_or_create_calendar(self, owner_name: str) -> str:
        """LEGACY (pre group-cutover): per-owner calendar resolution.  No
        longer called by the sync; kept for the misc/ rollback tooling."""
        if owner_name in self._cal_cache:
            return self._cal_cache[owner_name]
        summary = f"{owner_name} Portfolio"                      # new: no prefix
        legacy  = f"{CALENDAR_PREFIX} · {owner_name} Portfolio"  # pre-rename name
        page_token = None
        found_id = found_summary = None
        while True:
            resp = self.service.calendarList().list(pageToken=page_token).execute()
            for cal in resp.get("items", []):
                if cal["summary"] in (summary, legacy):
                    found_id, found_summary = cal["id"], cal["summary"]
                    break
            if found_id:
                break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if found_id:
            # Migrate a legacy "OKPM · …" calendar by renaming it in place so we
            # keep its events, ACLs, and ID (no orphaned duplicate calendar).
            if found_summary != summary:
                try:
                    self.service.calendars().patch(
                        calendarId=found_id, body={"summary": summary}).execute()
                    log.info(f"Renamed calendar: {found_summary!r} → {summary!r}")
                except HttpError as e:
                    log.warning(f"Could not rename calendar {found_id}: {e}")
            self._cal_cache[owner_name] = found_id
            return found_id
        cal = self.service.calendars().insert(body={
            "summary": summary,
            "description": (f"Rent tracking for {owner_name}'s portfolio. "
                            f"Do not edit — auto-synced from AppFolio."),
            "timeZone": "America/Chicago",
        }).execute()
        log.info(f"Created calendar: {summary}")
        self.created_calendar_ids.add(cal["id"])
        # PM gets owner so the calendar appears under 'My calendars'
        if PM_EMAIL:
            self._share(cal["id"], PM_EMAIL, role="owner", notify=False)
        self._cal_cache[owner_name] = cal["id"]
        return cal["id"]

    def _share(self, calendar_id: str, email: str, role: str = "reader", notify: bool = True):
        """Share calendar with email at role, updating the role if it changed."""
        if not email:
            return
        try:
            acl = self.service.acl().list(calendarId=calendar_id).execute()
            for rule in acl.get("items", []):
                if rule.get("scope", {}).get("value") == email:
                    if rule.get("role") != role:
                        # Role changed — update in place
                        self.service.acl().update(
                            calendarId=calendar_id,
                            ruleId=rule["id"],
                            body={"scope": {"type": "user", "value": email}, "role": role},
                            sendNotifications=False,
                        ).execute()
                        log.info(f"Updated calendar ACL for {email}: {rule.get('role')} → {role}")
                    return
            self.service.acl().insert(
                calendarId=calendar_id,
                body={"scope": {"type": "user", "value": email}, "role": role},
                sendNotifications=notify,
            ).execute()
            log.info(f"Shared calendar ({role}) with {email}")
        except HttpError as e:
            log.warning(f"Could not share with {email}: {e}")

    def ensure_pm_access(self, calendar_id: str):
        """PM gets OWNER so the calendar appears under 'My calendars' in Google
        Calendar (reader/writer land in 'Other calendars'). Owner is a superset
        of writer, so the PM can still drag events, manage commitments, etc."""
        self._share(calendar_id, PM_EMAIL, role="owner", notify=False)

    # ── Low-level event retrieval ─────────────────────────────────────────────

    def get_event(self, calendar_id: str, event_id: str) -> Optional[dict]:
        """Fetch a single event by ID. Returns None if deleted / not found."""
        try:
            return _gcal_execute(self.service.events().get(
                calendarId=calendar_id, eventId=event_id))
        except HttpError as e:
            if e.resp.status in (404, 410):
                return None
            raise

    def get_event_start_date(self, calendar_id: str, event_id: str) -> Optional[str]:
        """Return the ISO start date of an event, or None if the event is gone."""
        ev = self.get_event(calendar_id, event_id)
        if not ev:
            return None
        start = ev.get("start", {})
        return start.get("date") or start.get("dateTime", "")[:10]

    def find_all_events_by_type(
        self, calendar_id: str, occupancy_id: str, event_type: str,
    ) -> list[dict]:
        """
        Return ALL Google Calendar events matching (occupancy_id, event_type).
        Handles pagination and multiple events of the same type (split commitments).
        """
        items, page_token = [], None
        while True:
            resp = _gcal_execute(self.service.events().list(
                calendarId=calendar_id,
                privateExtendedProperty=[
                    f"okpm_occupancy_id={occupancy_id}",
                    f"okpm_event_type={event_type}",
                ],
                showDeleted=False,
                maxResults=100,
                pageToken=page_token,
            ))
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def list_all_events(self, calendar_id: str) -> tuple[dict, list]:
        """
        EVERY event on the calendar as (by_oid, untagged): okpm-tagged events
        grouped by bare occupancy_id, plus the untagged remainder (PM
        copy-paste copies — the Calendar UI strips extendedProperties.private
        — and any personal events, which the adoption classifier ignores).
        One paginated events().list with NO extended-property filter and NO
        time bounds: submit mode uses by_oid both as its event index and as
        its "is this event gone?" oracle, and _process_commitments treats a
        commitment missing from its listing as PM-deleted — a time window
        here would resurrect or duplicate out-of-window promises.
        """
        by_oid: dict = {}
        untagged: list = []
        page_token = None
        while True:
            resp = _gcal_execute(self.service.events().list(
                calendarId=calendar_id,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            ))
            for ev in resp.get("items", []):
                oid = (ev.get("extendedProperties", {})
                       .get("private", {})
                       .get("okpm_occupancy_id"))
                if oid:
                    by_oid.setdefault(str(oid), []).append(ev)
                else:
                    untagged.append(ev)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return by_oid, untagged

    def find_untagged_sync_candidates(self, calendar_id: str) -> list[dict]:
        """
        Untagged events — adoption-scan input.  The Calendar UI strips
        extendedProperties.private on copy, so PM copies of ANY sync event
        (commitment, status, payment, placeholder, NSF ghost) are blind to
        every okpm_* locator and freeze at the copied body.  One paginated
        UNFILTERED listing per calendar (a q= narrowing can't help: only
        commitment bodies carry the AUTO-SYNCED divider), client-filtered to
        events with no okpm_occupancy_id.  Classification (and the guard
        that leaves PM personal events alone) lives in
        transforms.classify_sync_copy — this is a pure listing.  Adoption
        re-tags the copies, so they self-exclude from the next scan
        (idempotent by construction).
        """
        copies, page_token = [], None
        while True:
            resp = _gcal_execute(self.service.events().list(
                calendarId=calendar_id,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            ))
            for ev in resp.get("items", []):
                props = ev.get("extendedProperties", {}).get("private", {})
                if not props.get("okpm_occupancy_id"):
                    copies.append(ev)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return copies

    # ── Commitment-specific operations ────────────────────────────────────────

    def convert_to_commitment(
        self,
        calendar_id: str,
        event_id: str,
        unit: dict,
        anchor_date: str,
        source_type: str,
        outstanding: float,
        source_status: str = "",
    ) -> dict:
        """
        Convert an existing movable event (kickstart or late) into a commitment
        event in-place.  Changes okpm_event_type to 'commitment', keeps the
        original colour/emoji, and adds the PM template above the divider.
        Returns the body written to the event (the event_id is unchanged) —
        submit mode records it as the event's post-conversion state.
        """
        pm_template = (
            "PROMISED: [fill in, e.g. $500 or 'full balance']\n"
            "NOTES:    [optional context]"
        )
        body = self._build_commitment_event(
            unit, anchor_date, source_type, outstanding,
            pm_notes=pm_template, source_status=source_status)
        # (start/end already set correctly by the builder: start=anchor_date,
        #  end=anchor_date+1 for a proper one-day all-day event)
        try:
            _gcal_execute(self.service.events().update(
                calendarId=calendar_id, eventId=event_id, body=body))
            log.info(
                f"  Converted event {event_id} → commitment on {anchor_date} "
                f"(source: {source_type}, outstanding: ${outstanding:,.2f})")
        except HttpError as e:
            log.error(f"  Failed to convert event {event_id}: {e}")
        return body

    def update_commitment_event(
        self,
        calendar_id: str,
        event_id: str,
        existing_body: dict,
        unit: dict,
        anchor_date: str,
        source_type: str,
        outstanding: float,
        source_status: str = "",
        breakdown: Optional[list] = None,
    ) -> str:
        """
        Update a commitment event while preserving PM notes above COMMITMENT_DIVIDER.
        existing_body is the pre-fetched Google event dict (avoids a redundant read).
        Returns the live anchor_date (PM may have re-dragged the event).
        """
        desc     = existing_body.get("description", "")
        pm_notes = (
            desc.split(COMMITMENT_DIVIDER)[0].rstrip()
            if COMMITMENT_DIVIDER in desc
            else desc
        )
        new_body = self._build_commitment_event(
            unit, anchor_date, source_type, outstanding,
            pm_notes=pm_notes, source_status=source_status, breakdown=breakdown)

        # Honour the live date: PM may have re-dragged the event
        live_date = existing_body.get("start", {}).get("date", anchor_date)
        new_body["start"]["date"] = live_date
        new_body["end"]["date"]   = _next_day(live_date)

        try:
            _gcal_execute(self.service.events().update(
                calendarId=calendar_id, eventId=event_id, body=new_body))
        except HttpError as e:
            log.error(f"  Failed to update commitment {event_id}: {e}")
        return live_date

    def revert_event_to_date(
        self, calendar_id: str, event_id: str, canonical_date: str,
    ):
        """
        Soft-lock enforcement: read the live event date; if it differs from
        canonical_date, move it back.  No-op if already correct or event is gone.
        """
        ev = self.get_event(calendar_id, event_id)
        if not ev:
            return
        live_date = (ev.get("start", {}).get("date")
                     or ev.get("start", {}).get("dateTime", "")[:10])
        if live_date == canonical_date:
            return
        log.warning(f"  REVERT locked event {event_id}: {live_date} → {canonical_date}")
        ev["start"] = {"date": canonical_date}
        ev["end"]   = {"date": _next_day(canonical_date)}
        try:
            _gcal_execute(self.service.events().update(
                calendarId=calendar_id, eventId=event_id, body=ev))
        except HttpError as e:
            log.error(f"  Failed to revert event {event_id}: {e}")

    # ── NSF reversal mutations (prior-month reconciliation) ──────────────────

    def flip_event_to_nsf(self, calendar_id: str, event_body: dict,
                          note_line: str, retag_idx: bool = False):
        """
        Repaint an event whose payment was later reversed: red, ' NSF' title
        tag, reversal note appended.  The Status: line is rewritten to
        '🔴 REVERSED / NSF' — it reads as a current verdict, so leaving
        '✅ Paid' inside a red NSF event would contradict the reversal — and
        the historical Received/Balance lines are stamped '(before reversal)'
        (their numbers were true when written; June's running balances are
        not reconstructable, so they stay visible but time-stamped).
        Idempotent (no-op when already fully flipped).  retag_idx moves
        okpm_payment_idx aside (idx → 'nsf<idx>') so _find_payment_event's
        exact-index matching can never collide with a future payment at that
        position.
        """
        summary   = event_body.get("summary") or ""
        desc      = event_body.get("description") or ""
        status_ok = not re.search(r"^Status:(?!.*REVERSED).*$", desc, re.M)
        if (event_body.get("colorId") == COLOR_UNPAID
                and " NSF" in summary and note_line in desc and status_ok):
            return
        parts = summary.split(" · ", 1)
        if len(parts) == 2:
            summary = "🔴 · " + parts[1]
        if " NSF" not in summary:
            summary += " NSF"
        lines = []
        for line in desc.splitlines():
            s = line.strip()
            if s.startswith("Status:") and "REVERSED" not in line:
                line = "Status:       🔴 REVERSED / NSF"
            elif (s.startswith(("Received in", "Balance after this payment:",
                                "Balance:"))
                    and "(before reversal)" not in line):
                line = line + "  (before reversal)"
            lines.append(line)
        desc = "\n".join(lines)
        if note_line not in desc:
            desc = (desc + "\n" if desc else "") + note_line
        event_body["summary"]     = summary
        event_body["description"] = desc
        event_body["colorId"]     = COLOR_UNPAID
        if retag_idx:
            props = (event_body.setdefault("extendedProperties", {})
                     .setdefault("private", {}))
            idx = props.get("okpm_payment_idx")
            if idx is not None and not str(idx).startswith("nsf"):
                props["okpm_payment_idx"] = f"nsf{idx}"
            props["okpm_nsf"] = "1"
        try:
            _gcal_execute(self.service.events().update(
                calendarId=calendar_id, eventId=event_body["id"],
                body=event_body))
        except HttpError as e:
            log.error(f"  Failed to flip event {event_body.get('id')} to NSF: {e}")

    def unmute_event_to_own_status(self, calendar_id: str, event_body: dict):
        """
        Un-grey a settled-muted event after its month's settlement broke
        (a payment was reversed).  NSF markers take precedence (red — the
        body's own 'Status:' line reads 🟡 for NSF payments, which would be
        wrong); else the event's own Status line decides; unparseable /
        unknown → left grey with a log line.

        LEGACY-MONTHS ONLY since the settled-collapse deploy: new months
        never render grey (a fully-paid month collapses to one event
        instead), so this now serves only pre-deploy months whose grey
        events are frozen history.
        """
        if event_body.get("colorId") != COLOR_SETTLED:
            return
        summary = event_body.get("summary") or ""
        desc    = event_body.get("description") or ""
        if " NSF" in summary or "REVERSED" in desc:
            color, emoji = COLOR_UNPAID, "🔴"
        else:
            status = parse_status_line(desc)
            if status not in (STATUS_PAID, STATUS_PREPAID, STATUS_PARTIAL,
                              STATUS_UNPAID, STATUS_LATE):
                log.warning(
                    f"  Cannot un-grey event {event_body.get('id')}: "
                    f"unrecognized Status line {status!r} — leaving muted")
                return
            color, emoji = color_for_status(status), emoji_for_status(status)
        parts = summary.split(" · ", 1)
        if len(parts) == 2:
            event_body["summary"] = f"{emoji} · " + parts[1]
        event_body["colorId"] = color
        try:
            _gcal_execute(self.service.events().update(
                calendarId=calendar_id, eventId=event_body["id"],
                body=event_body))
        except HttpError as e:
            log.error(f"  Failed to un-grey event {event_body.get('id')}: {e}")

    def append_description_note(self, calendar_id: str, event_body: dict,
                                note_line: str):
        """Append a note line to an event's description (idempotent)."""
        desc = event_body.get("description") or ""
        if note_line in desc:
            return
        event_body["description"] = (desc + "\n" if desc else "") + note_line
        try:
            _gcal_execute(self.service.events().update(
                calendarId=calendar_id, eventId=event_body["id"],
                body=event_body))
        except HttpError as e:
            log.error(f"  Failed to append note to {event_body.get('id')}: {e}")

    # ── Event builders ────────────────────────────────────────────────────────

    def _build_commitment_event(
        self,
        unit: dict,
        anchor_date: str,
        source_type: str,
        outstanding: float,
        pm_notes: str = "",
        source_status: str = "",
        breakdown: Optional[list] = None,
    ) -> dict:
        """
        Commitment (promise-to-pay) event.
        Keeps the original color/emoji of the source event so the PM sees
        a familiar visual at the promised date.

        Description layout:
          <PM-editable section — everything above COMMITMENT_DIVIDER>
          ── AUTO-SYNCED — do not edit below ──
          Tenant / address / phone
          Monthly Rent / Outstanding / Status
          Commitment date / Source / Last Synced
        """
        try:
            display_date = date.fromisoformat(anchor_date).strftime("%b %d, %Y")
        except ValueError:
            display_date = anchor_date

        clamp_outstanding = max(0.0, outstanding)
        # Use the source event's status for colour/emoji; fall back to computed
        if not source_status:
            source_status = classify_status(unit["rent"], outstanding)
        emoji      = emoji_for_status(source_status)
        color      = color_for_status(source_status)
        unit_part  = f"{unit['unit_label']} · " if unit["unit_label"] else ""
        tenant_name = normalize_tenant_name(unit["tenant"])

        title = (
            f"{emoji} · {tenant_name} · {unit_part}{unit['property_name']} · "
            f"${clamp_outstanding:,.0f} owed · Promise {display_date}"
        )

        if not pm_notes.strip():
            pm_notes = (
                "PROMISED: [fill in, e.g. $500 or 'full balance']\n"
                "NOTES:    [optional context]"
            )

        # When a promise was dragged into a future month, `outstanding` is the
        # COMBINED total (everything owed now + rent accruing by the promised
        # date). Itemise it so the PM sees what makes up the number.
        if breakdown:
            outstanding_lines = [f"Outstanding:  ${clamp_outstanding:,.2f}  (combined)"]
            outstanding_lines += [
                f"   • {label}: ${max(0.0, amt):,.2f}" for label, amt in breakdown
            ]
        else:
            outstanding_lines = [f"Outstanding:  ${clamp_outstanding:,.2f}"]

        auto_lines = [
            f"Tenant:       {normalize_tenant_name(unit['tenant'])}",
            ((f"{unit['unit_label']}  |  ") if unit["unit_label"] else "") + unit["address"],
            f"Phone:        {unit['phone']}",
            "─" * 44,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            *outstanding_lines,
            f"Status:       {source_status}",
            "─" * 44,
            f"Committed:    {display_date}",
            f"Source:       {dict(kickstart='Kickstart (future rent)', late='Preview/late (arrears)', status='Status event (dragged)', payment='Payment event (dragged)').get(source_type, source_type)}",
            f"Late Fee:     {unit.get('late_fee_desc', 'N/A')}",
            f"Lease:        {unit['lease_from']} → {unit['lease_to']}",
            f"Last Synced:  {date.today().strftime('%b %d, %Y')}",
        ]

        description = f"{pm_notes}\n{COMMITMENT_DIVIDER}\n" + "\n".join(auto_lines)

        return {
            "summary":     title,
            "location":    unit["address"],
            "description": description,
            "start":       {"date": anchor_date},
            "end":         {"date": _next_day(anchor_date)},
            "colorId":     color,
            "extendedProperties": {"private": {
                "okpm_occupancy_id":  str(unit["occupancy_id"]),
                "okpm_month":         anchor_date[:7],
                "okpm_event_type":    "commitment",
                "okpm_source_type":   source_type,
            }},
        }

    def _build_status_event(
        self, unit: dict, event_status: str, event_date: date,
        first_payment: Optional[dict] = None,
        balance_after_first: Optional[float] = None,
        total_payments: int = 0,
        reversal_notes: Optional[list] = None,
        promise_history: Optional[list] = None,
        settled_prefix: Optional[dict] = None,
    ) -> dict:
        """
        first_payment is the month's first DAY-GROUP (all payments on the
        first payment date, absorbed into this event); total_payments counts
        day-groups.  A fully-paid month uses _build_settled_month_event
        instead — the old grey muting is gone.  settled_prefix renders the
        "previously settled" section on a REACTIVATED month (a charge after
        settlement being paid down): {count, total, settled_on, rows}.
        """
        emoji        = emoji_for_status(event_status)
        title_color  = color_for_status(event_status)
        # An NSF first payment reads red — nothing was effectively received —
        # matching the explicit override in _build_additional_payment_event.
        # (event_status comes from payment_status, which never returns
        # Unpaid, so without this an NSF first payment rendered yellow.)
        if first_payment and first_payment.get("is_nsf"):
            emoji, title_color = "🔴", COLOR_UNPAID
        unit_part    = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = normalize_tenant_name(unit['tenant'])
        tenant_full  = normalize_tenant_name(unit['tenant'])
        tenants      = tenant_full
        if unit.get('additional_tenants'):
            tenants += f", {normalize_tenant_name(unit['additional_tenants'])}"

        outstanding = max(0.0, unit['past_due'])
        has_credit  = unit['past_due'] < 0

        if first_payment:
            nsf_tag = " NSF" if first_payment['is_nsf'] else ""
            title = (
                f"{emoji} · {tenant_short} · "
                f"{unit_part}{unit['property_name']} · "
                f"${first_payment['amount']:,.0f} paid{nsf_tag}"
            )
        else:
            title = (
                f"{emoji} · {tenant_short} · "
                f"{unit_part}{unit['property_name']} · "
                f"${outstanding:,.0f} due"
            )

        due_date_this_month = date(event_date.year, event_date.month, RENT_DUE_DAY)
        late_after = (
            due_date_this_month + timedelta(days=unit.get('grace_days', LATE_GRACE_DAYS))
        ).strftime('%b %d, %Y')

        desc = [
            f"Tenant(s):    {tenants}",
            (f"{unit['unit_label']}  |  " if unit['unit_label'] else "") + unit['address'],
            f"Phone:        {unit['phone']}",
            "─" * 40,
            f"Monthly Rent: ${unit['rent']:,.2f}",
        ]

        # REACTIVATED month: the settled payments live on as history text
        # only — the fresh tracking below covers just the new balance.
        if settled_prefix and settled_prefix.get("count"):
            settled_on = settled_prefix.get("settled_on") or ""
            try:
                settled_on = date.fromisoformat(settled_on).strftime("%b %d, %Y")
            except ValueError:
                pass
            desc += [
                "─" * 40,
                (f"Previously settled {settled_on}: "
                 f"${settled_prefix['total']:,.2f} across "
                 f"{settled_prefix['count']} payment(s)"),
            ]
            desc += self._payment_history_blocks(settled_prefix.get("rows") or [])
            desc.append("A charge posted after settlement — tracking below "
                        "covers the new balance only.")

        if first_payment:
            rows = _payment_rows(first_payment)
            try: month_label = date.fromisoformat(first_payment['date']).strftime('%B')
            except: month_label = 'this month'
            bal            = balance_after_first if balance_after_first is not None else unit['past_due']
            remaining      = max(0.0, bal)
            has_credit_now = bal < 0
            header = f"Payment {1} of {total_payments}"
            if len(rows) > 1:
                header += f"  ({len(rows)} same-day payments)"
            desc += ["─" * 40, header]
            for r in rows:
                try: pay_display = date.fromisoformat(r['date']).strftime('%b %d, %Y')
                except: pay_display = r['date']
                nsf_note = "  ⚠️ REVERSED / NSF" if r.get('is_nsf') else ""
                desc += [
                    f"Date:         {pay_display}",
                    f"Method:       {r['description']}{nsf_note}",
                    f"Amount:       ${r['amount']:,.2f}",
                ]
                intended = r.get('intended_month')
                if intended:
                    desc.append(
                        f"              ⚠️ Applies to "
                        f"{date(intended[0],intended[1],1).strftime('%B %Y')} charges"
                    )
            credit_suffix = (
                f"  (+ ${abs(bal):,.2f} credit toward next month)" if has_credit_now else ""
            )
            desc += [
                "─" * 40,
                f"Received in {month_label}: ${unit['amount_paid']:,.2f}",
                f"Monthly Rent: ${unit['rent']:,.2f}",
                f"Balance:      ${remaining:,.2f}{credit_suffix}",
                f"Status:       {event_status}",
            ]
        else:
            balance_line = (
                f"Outstanding:  $0.00  (+ ${abs(unit['past_due']):,.2f} credit toward next month)"
                if has_credit else
                f"Outstanding:  ${outstanding:,.2f}"
            )
            desc += [
                balance_line,
                f"Status:       {event_status}",
                "─" * 40,
                "No payments received yet.",
            ]

        if promise_history:
            desc += ["─" * 40] + _promise_history_lines(promise_history)

        if reversal_notes:
            desc += ["─" * 40] + [str(n) for n in reversal_notes]

        desc += [
            "─" * 40,
            f"Late Fee:     {unit.get('late_fee_desc','N/A')}",
            "              (Balance includes any applied/waived fees.)",
            f"Late After:   {late_after}",
            f"Lease:        {unit['lease_from']} → {unit['lease_to']}",
        ]

        return {
            "summary":     title,
            "location":    unit['address'],
            "description": "\n".join(desc),
            "start":       {"date": event_date.isoformat()},
            "end":         {"date": _next_day(event_date.isoformat())},
            "colorId":     title_color,
            "extendedProperties": {"private": {
                "okpm_occupancy_id": str(unit['occupancy_id']),
                "okpm_month":        event_date.strftime("%Y-%m"),
                "okpm_event_type":   "status",
            }},
        }

    def _build_additional_payment_event(
        self, unit: dict, payment: dict,
        payment_num: int, total_payments: int,
        running_balance: float, month_received: float,
    ) -> dict:
        """payment is a DAY-GROUP (all of one date's payments as one event;
        NSF rows always arrive as singleton groups); payment_num /
        total_payments count day-groups.  A fully-paid month has no payment
        events at all (see _build_settled_month_event) — the old grey muting
        is gone."""
        pay_date   = payment["date"]
        rows       = _payment_rows(payment)
        # This event represents a received payment → payment_status keeps it
        # 🟡 Partial (never 🔴) while any balance remains.  (NSF and intended-
        # month cases below override the colour explicitly.)
        pay_status = payment_status(unit['rent'], running_balance)
        pay_emoji  = emoji_for_status(pay_status)
        try: month_label = date.fromisoformat(pay_date).strftime("%B")
        except: month_label = "this month"

        tenant_full = normalize_tenant_name(unit['tenant'])
        unit_part   = f"{unit['unit_label']} · " if unit['unit_label'] else ""

        if payment['is_nsf']:
            emoji, color, tag = "🔴", COLOR_UNPAID, " NSF"
        elif payment.get('intended_month'):
            emoji, color, tag = "🟡", COLOR_PARTIAL, " (late)"
        else:
            emoji, color, tag = pay_emoji, color_for_status(pay_status), ""

        title = (
            f"{emoji} · {tenant_full} · "
            f"{unit_part}{unit['property_name']} · "
            f"${payment['amount']:,.0f}{tag}"
        )

        bal_display = max(0.0, running_balance)
        has_credit  = running_balance < 0

        header = f"Payment {payment_num} of {total_payments} in {month_label}"
        if len(rows) > 1:
            header += f"  ({len(rows)} same-day payments)"
        desc = [header]
        for r in rows:
            try: pay_display = date.fromisoformat(r['date']).strftime("%b %d, %Y")
            except: pay_display = r['date']
            nsf_note = "  ⚠️ REVERSED / NSF" if r.get('is_nsf') else ""
            desc += [
                f"Date:         {pay_display}",
                f"Method:       {r['description']}{nsf_note}",
                f"Amount:       ${r['amount']:,.2f}",
            ]
            intended = r.get('intended_month')
            if intended:
                desc.append(
                    f"              ⚠️ Applies to "
                    f"{date(intended[0],intended[1],1).strftime('%B %Y')} charges"
                )
        desc += [
            "─" * 40,
            f"Received in {month_label}: ${month_received:,.2f}",
            f"Monthly Rent: ${unit['rent']:,.2f}",
            (f"Balance after this payment: ${bal_display:,.2f}"
             + (f"  (+ ${abs(running_balance):,.2f} credit)" if has_credit else "")),
            f"Status:       {pay_status}",
            "─" * 40,
            f"Tenant:       {tenant_full}",
            (f"{unit['unit_label']}  |  " if unit['unit_label'] else "") + unit['address'],
            f"Phone:        {unit['phone']}",
        ]

        return {
            "summary":     title,
            "location":    unit['address'],
            "description": "\n".join(desc),
            "start":       {"date": pay_date},
            "end":         {"date": _next_day(pay_date)},
            "colorId":     color,
            "extendedProperties": {"private": {
                "okpm_occupancy_id":  str(unit['occupancy_id']),
                "okpm_month":         pay_date[:7],
                "okpm_event_type":    "payment",
                "okpm_payment_idx":   str(payment_num - 1),
            }},
        }

    @staticmethod
    def _payment_history_blocks(rows: list[dict]) -> list[str]:
        """Column-0 Date/Method/Amount blocks for retired (settled) payment
        rows.  The exact 'Amount:       $X' format is load-bearing: NSF
        reversal matching greps these lines (see _reversal_matches_text)."""
        lines: list[str] = []
        total = len(rows)
        for i, r in enumerate(rows, start=1):
            try:
                pay_display = date.fromisoformat(r.get("date") or "").strftime("%b %d, %Y")
            except ValueError:
                pay_display = r.get("date") or "?"
            nsf_note = "  ⚠️ REVERSED / NSF" if r.get("is_nsf") else ""
            lines += [
                f"Payment {i} of {total}",
                f"Date:         {pay_display}",
                f"Method:       {r.get('description','')}{nsf_note}",
                f"Amount:       ${float(r.get('amount') or 0):,.2f}",
            ]
        return lines

    def _build_settled_month_event(
        self, unit: dict, anchor_date: date, day_groups: list,
        promise_history: Optional[list] = None,
        reversal_notes: Optional[list] = None,
        settled_on: Optional[str] = None,
    ) -> dict:
        """
        The ONE event a fully-paid month collapses to: green (balance 0) or
        pink (credit) on the LAST payment date, with the whole payment and
        promise history itemised in the description — the individual payment
        events are deleted.  Keeps okpm_event_type="status" so every finder
        and self-heal path treats it as the month's status event.  unit's
        past_due/amount_paid must describe the settled snapshot (a FROZEN
        rebuild passes the stored settled_past_due, not the live balance).
        """
        event_status = classify_status(unit["rent"], unit["past_due"])
        emoji        = emoji_for_status(event_status)
        color        = color_for_status(event_status)
        unit_part    = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = normalize_tenant_name(unit['tenant'])
        tenants      = tenant_short
        if unit.get('additional_tenants'):
            tenants += f", {normalize_tenant_name(unit['additional_tenants'])}"

        rows        = [r for g in day_groups for r in _payment_rows(g)]
        month_total = sum(r["amount"] for r in rows if not r.get("is_nsf"))
        has_credit  = unit["past_due"] < 0
        month_label = anchor_date.strftime("%B")

        if rows:
            title_tail = f"${month_total:,.0f} paid"
        else:
            # Pure-prepaid month: nothing arrived this month, the credit
            # already covers it — same "$0 due" shape as the plain builder.
            title_tail = "$0 due"
        title = (
            f"{emoji} · {tenant_short} · "
            f"{unit_part}{unit['property_name']} · {title_tail}"
        )

        settled_display = settled_on or ""
        try:
            settled_display = date.fromisoformat(settled_display).strftime("%b %d, %Y")
        except ValueError:
            pass

        due_date_this_month = date(anchor_date.year, anchor_date.month, RENT_DUE_DAY)
        late_after = (
            due_date_this_month + timedelta(days=unit.get('grace_days', LATE_GRACE_DAYS))
        ).strftime('%b %d, %Y')

        credit_suffix = (
            f"  (+ ${abs(unit['past_due']):,.2f} credit toward next month)"
            if has_credit else ""
        )
        desc = [
            f"Tenant(s):    {tenants}",
            (f"{unit['unit_label']}  |  " if unit['unit_label'] else "") + unit['address'],
            f"Phone:        {unit['phone']}",
            "─" * 40,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Received in {month_label}: ${month_total:,.2f}",
            f"Balance:      $0.00{credit_suffix}",
            f"Status:       {event_status}",
        ]
        if settled_display:
            desc.append(f"Settled:      {settled_display}")
        if rows:
            desc += ["─" * 40,
                     f"Payment history ({len(rows)} payment(s), consolidated):"]
            desc += self._payment_history_blocks(rows)
        if promise_history:
            desc += ["─" * 40] + _promise_history_lines(promise_history)
        if reversal_notes:
            desc += ["─" * 40] + [str(n) for n in reversal_notes]
        desc += [
            "─" * 40,
            f"Late Fee:     {unit.get('late_fee_desc','N/A')}",
            f"Late After:   {late_after}",
            f"Lease:        {unit['lease_from']} → {unit['lease_to']}",
        ]

        return {
            "summary":     title,
            "location":    unit['address'],
            "description": "\n".join(desc),
            "start":       {"date": anchor_date.isoformat()},
            "end":         {"date": _next_day(anchor_date.isoformat())},
            "colorId":     color,
            "extendedProperties": {"private": {
                "okpm_occupancy_id": str(unit['occupancy_id']),
                "okpm_month":        anchor_date.strftime("%Y-%m"),
                "okpm_event_type":   "status",
            }},
        }

    def _build_nsf_ghost_event(
        self, unit: dict, stored_row: dict, note_line: str, month: str,
    ) -> dict:
        """
        Reconstructed red event for a bounced payment whose positive ledger
        row VANISHED from the pull (typical for NSF) after the month had
        already collapsed — without it, a reverted collapse would show no
        trace of the failed payment.  Honesty convention: no Received/Balance
        lines (running balances at that moment are not reconstructable), and
        the description says the event was rebuilt from sync records.
        okpm_payment_idx="nsfg" is non-numeric on purpose: it can never
        collide with _find_payment_event's exact-index lookups.
        """
        pay_date = stored_row.get("date") or f"{month}-01"
        try:
            pay_display = date.fromisoformat(pay_date).strftime("%b %d, %Y")
        except ValueError:
            pay_display, pay_date = pay_date, f"{month}-01"
        amount      = float(stored_row.get("amount") or 0)
        tenant_full = normalize_tenant_name(unit['tenant'])
        unit_part   = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        title = (
            f"🔴 · {tenant_full} · "
            f"{unit_part}{unit['property_name']} · "
            f"${amount:,.0f} NSF"
        )
        desc = [
            "Reversed payment (NSF)",
            f"Date:         {pay_display}",
            f"Method:       {stored_row.get('description','')}  ⚠️ REVERSED / NSF",
            f"Amount:       ${amount:,.2f}",
            "Status:       🔴 REVERSED / NSF",
            note_line,
            "─" * 40,
            "Reconstructed from sync records after the ledger row was "
            "reversed; see the status event for the month's balance.",
            "─" * 40,
            f"Tenant:       {tenant_full}",
            (f"{unit['unit_label']}  |  " if unit['unit_label'] else "") + unit['address'],
            f"Phone:        {unit['phone']}",
        ]
        return {
            "summary":     title,
            "location":    unit['address'],
            "description": "\n".join(desc),
            "start":       {"date": pay_date},
            "end":         {"date": _next_day(pay_date)},
            "colorId":     COLOR_UNPAID,
            "extendedProperties": {"private": {
                "okpm_occupancy_id":  str(unit['occupancy_id']),
                "okpm_month":         month,
                "okpm_event_type":    "payment",
                "okpm_payment_idx":   "nsfg",
                "okpm_nsf":           "1",
            }},
        }

    def _build_future_placeholder(self, unit: dict, status: str, due_date: date) -> dict:
        """Frozen future-month event on the 1st."""
        emoji        = emoji_for_status(status)
        unit_part    = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = normalize_tenant_name(unit['tenant'])
        outstanding  = max(0.0, unit['past_due'])
        title = (
            f"{emoji} · {tenant_short} · "
            f"{unit_part}{unit['property_name']} · "
            f"${outstanding:,.0f} due"
        )
        late_after = (
            due_date + timedelta(days=unit.get('grace_days', LATE_GRACE_DAYS))
        ).strftime('%b %d, %Y')
        desc = [
            f"Tenant(s):    {normalize_tenant_name(unit['tenant'])}",
            (f"{unit['unit_label']}  |  " if unit['unit_label'] else "") + unit['address'],
            f"Phone:        {unit['phone']}",
            "─" * 40,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Outstanding:  ${outstanding:,.2f}",
            f"Status:       {status}",
            "─" * 40,
            f"Late Fee:     {unit.get('late_fee_desc','N/A')}",
            f"Late After:   {late_after}",
            f"Lease:        {unit['lease_from']} → {unit['lease_to']}",
        ]
        if unit['past_due'] < 0:
            desc.insert(7, f"              Credit: ${abs(unit['past_due']):,.2f} toward this month")
        return {
            "summary":     title,
            "location":    unit['address'],
            "description": "\n".join(desc),
            "start":       {"date": due_date.isoformat()},
            "end":         {"date": _next_day(due_date.isoformat())},
            "colorId":     color_for_status(status),
            "extendedProperties": {"private": {
                "okpm_occupancy_id": str(unit['occupancy_id']),
                "okpm_month":        due_date.strftime("%Y-%m"),
                "okpm_event_type":   "rent",
            }},
        }

    def _build_moved_out_event(self, identity: dict, anchor_iso: str,
                               moved_line: str, cleaned_date: date) -> dict:
        """One neutral marker per calendar for a departed occupancy.  NO
        balance figure anywhere: once the tenant leaves the roll AppFolio
        shows us nothing, so any number would be a stale claim — the
        description points at AppFolio instead."""
        unit_part = (f"{identity['unit_label']} · "
                     if identity.get("unit_label") else "")
        prop = identity.get("property_name") or identity.get("address") or ""
        title = (f"📦 · {identity.get('tenant') or 'Unknown tenant'} · "
                 f"{unit_part}{prop} · moved out")
        desc = [
            moved_line,
            f"Events cleaned: {cleaned_date.strftime('%b %d, %Y')}",
            "Final ledger lives in AppFolio.",
        ]
        if identity.get("note"):
            desc.append(identity["note"])
        return {
            "summary":     title,
            "location":    identity.get("address", ""),
            "description": "\n".join(desc),
            "start":       {"date": anchor_iso},
            "end":         {"date": _next_day(anchor_iso)},
            "colorId":     COLOR_SETTLED,   # graphite — neutral, non-status
            "extendedProperties": {"private": {
                "okpm_occupancy_id": str(identity.get("occupancy_id", "")),
                "okpm_month":        anchor_iso[:7],
                "okpm_event_type":   "moved_out",
            }},
        }

    # ── Event find / upsert / delete ─────────────────────────────────────────
    # All Google API calls below use _gcal_execute() for retry on rate limits.

    def _find_event(
        self, calendar_id: str, occupancy_id: str, month: str, event_type: str,
    ) -> Optional[str]:
        # Events are tagged with the RAW occupancy_id from AppFolio
        # (unit["occupancy_id"]), never the scoped soid.  Strip the
        # "@g{group_id}" (formerly "@owner_id") suffix so the search matches
        # the tag on the event.
        search_oid = occupancy_id.split("@")[0] if "@" in occupancy_id else occupancy_id
        result = _gcal_execute(self.service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=[
                f"okpm_occupancy_id={search_oid}",
                f"okpm_month={month}",
                f"okpm_event_type={event_type}",
            ],
        ))
        items = result.get("items", [])
        # Auto-clean any duplicates that slipped through during the soid migration.
        for dup in items[1:]:
            try:
                _gcal_execute(self.service.events().delete(
                    calendarId=calendar_id, eventId=dup["id"]))
                log.warning(
                    f"  Deduped: deleted extra {event_type} event "
                    f"for oid={occupancy_id} on {month}")
            except Exception:
                pass
        return items[0]["id"] if items else None

    def _find_status_event(
        self, calendar_id: str, occupancy_id: str, month: str,
    ) -> Optional[str]:
        """Falls back to old 'rent' type for backward compat."""
        return (
            self._find_event(calendar_id, occupancy_id, month, "status") or
            self._find_event(calendar_id, occupancy_id, month, "rent")
        )

    def _find_payment_event(
        self, calendar_id: str, occupancy_id: str, month: str, idx: int,
    ) -> Optional[str]:
        result = _gcal_execute(self.service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=[
                f"okpm_occupancy_id={occupancy_id}",
                f"okpm_month={month}",
                f"okpm_event_type=payment",
                f"okpm_payment_idx={idx}",
            ],
        ))
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def find_month_events(
        self, calendar_id: str, occupancy_id: str, month: str,
        event_type: str,
    ) -> list[dict]:
        """All of a unit's events of one type for one month, FULL bodies
        (paginated extended-property query).  Bodies, not just ids, so the
        covered-month cleanup can compare live starts before deleting — an
        event mid-drag must be left for drag detection, never deleted."""
        search_oid = (occupancy_id.split("@")[0]
                      if "@" in occupancy_id else occupancy_id)
        items, page_token = [], None
        while True:
            resp = _gcal_execute(self.service.events().list(
                calendarId=calendar_id,
                privateExtendedProperty=[
                    f"okpm_occupancy_id={search_oid}",
                    f"okpm_month={month}",
                    f"okpm_event_type={event_type}",
                ],
                maxResults=250, pageToken=page_token,
            ))
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def find_month_payment_events(
        self, calendar_id: str, occupancy_id: str, month: str,
    ) -> list[dict]:
        """All of a unit's payment-typed events for one month, FULL bodies —
        numeric-idx markers, nsf-retagged flips, and "nsfg" ghosts alike.
        Feeds the surplus cleanup so strays are discoverable even when state
        lost their ids (deep-clean runs only; not part of the hourly path)."""
        return self.find_month_events(
            calendar_id, occupancy_id, month, "payment")

    def _update_or_create(
        self, calendar_id: str, event_id: Optional[str], body: dict,
    ) -> str:
        if event_id:
            try:
                _gcal_execute(self.service.events().update(
                    calendarId=calendar_id, eventId=event_id, body=body))
                return event_id
            except HttpError as e:
                if e.resp.status in (404, 410):
                    log.warning(
                        f"  Event {event_id} gone (HTTP {e.resp.status}) "
                        f"— creating replacement")
                else:
                    raise
        created = _gcal_execute(self.service.events().insert(
            calendarId=calendar_id, body=body))
        return created["id"]

    def upsert_event(self, calendar_id: str, event_body: dict) -> str:
        props    = event_body["extendedProperties"]["private"]
        existing = self._find_event(
            calendar_id,
            props["okpm_occupancy_id"],
            props["okpm_month"],
            props["okpm_event_type"],
        )
        return self._update_or_create(calendar_id, existing, event_body)

    def delete_event(self, calendar_id: str, event_id: str):
        try:
            _gcal_execute(self.service.events().delete(
                calendarId=calendar_id, eventId=event_id))
            log.info(f"Deleted event {event_id}")
        except HttpError as e:
            if e.resp.status != 410:
                raise
