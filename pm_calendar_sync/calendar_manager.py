"""Google Calendar manager: calendar/event CRUD and event builders."""
import json
import time
from datetime import date, timedelta
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    GOOGLE_SA_JSON, GOOGLE_SCOPES, PM_EMAIL, CALENDAR_PREFIX,
    RENT_DUE_DAY, LATE_GRACE_DAYS, COMMITMENT_DIVIDER,
    COLOR_PARTIAL, COLOR_UNPAID, COLOR_SETTLED,
    GCAL_RETRY_ATTEMPTS, GCAL_RETRY_BASE_DELAY, log,
)
from .status import (
    classify_status, payment_status, color_for_status, emoji_for_status,
    STATUS_PARTIAL, STATUS_UNPAID, STATUS_SETTLED,
)
from .transforms import normalize_tenant_name, _next_day


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


class GoogleCalendarManager:

    def __init__(self):
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_SA_JSON), scopes=GOOGLE_SCOPES)
        self.service = build("calendar", "v3", credentials=creds)
        self._cal_cache: dict = {}

    # ── Calendar management ──────────────────────────────────────────────────

    def get_or_create_calendar(self, owner_name: str) -> str:
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

    def share_with_owner(self, calendar_id: str, email: str):
        """Owners stay reader-only; they should not move events."""
        self._share(calendar_id, email, role="reader", notify=True)

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
    ) -> str:
        """
        Convert an existing movable event (kickstart or late) into a commitment
        event in-place.  Changes okpm_event_type to 'commitment', keeps the
        original colour/emoji, and adds the PM template above the divider.
        Returns event_id (unchanged).
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
        return event_id

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
            pm_notes=pm_notes, source_status=source_status)

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

    # ── Event builders ────────────────────────────────────────────────────────

    def _build_commitment_event(
        self,
        unit: dict,
        anchor_date: str,
        source_type: str,
        outstanding: float,
        pm_notes: str = "",
        source_status: str = "",
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

        auto_lines = [
            f"Tenant:       {normalize_tenant_name(unit['tenant'])}",
            ((f"{unit['unit_label']}  |  ") if unit["unit_label"] else "") + unit["address"],
            f"Phone:        {unit['phone']}",
            "─" * 44,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Outstanding:  ${clamp_outstanding:,.2f}",
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
        month_fully_paid: bool = False,
    ) -> dict:
        emoji        = emoji_for_status(event_status)
        # Once the month is fully paid, mute an earlier/partial headline event to
        # grey so attention stays on units that still owe. A single payment that
        # settles the month has event_status Paid (green) and prepaid has Prepaid
        # (pink) — neither is Partial/Unpaid, so neither is muted here.
        title_color  = color_for_status(event_status)
        if month_fully_paid and event_status in (STATUS_PARTIAL, STATUS_UNPAID):
            emoji       = emoji_for_status(STATUS_SETTLED)
            title_color = COLOR_SETTLED
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

        if first_payment:
            try: pay_display = date.fromisoformat(first_payment['date']).strftime('%b %d, %Y')
            except: pay_display = first_payment['date']
            try: month_label = date.fromisoformat(first_payment['date']).strftime('%B')
            except: month_label = 'this month'
            nsf_note      = "  ⚠️ REVERSED / NSF" if first_payment['is_nsf'] else ""
            intended       = first_payment.get('intended_month')
            bal            = balance_after_first if balance_after_first is not None else unit['past_due']
            remaining      = max(0.0, bal)
            has_credit_now = bal < 0
            desc += [
                "─" * 40,
                f"Payment {1} of {total_payments}",
                f"Date:         {pay_display}",
                f"Method:       {first_payment['description']}{nsf_note}",
                f"Amount:       ${first_payment['amount']:,.2f}",
            ]
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
        month_fully_paid: bool = False,
    ) -> dict:
        pay_date   = payment["date"]
        # This event represents a received payment → payment_status keeps it
        # 🟡 Partial (never 🔴) while any balance remains.  (NSF and intended-
        # month cases below override the colour explicitly.)
        pay_status = payment_status(unit['rent'], running_balance)
        pay_emoji  = emoji_for_status(pay_status)
        try: pay_display = date.fromisoformat(pay_date).strftime("%b %d, %Y")
        except: pay_display = pay_date
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

        # Once the month is fully paid, mute earlier or failed (NSF) payments to
        # grey so the PM's attention stays on units that still owe.  The settling
        # payment (balance 0 → Paid/green) and any prepaid/credit payment (Prepaid/
        # pink) leave running_balance <= 0, so pay_status is not Partial and they
        # keep their colour — marking exactly when the tenant paid in full.
        if month_fully_paid and (payment['is_nsf'] or pay_status == STATUS_PARTIAL):
            emoji, color = emoji_for_status(STATUS_SETTLED), COLOR_SETTLED

        title = (
            f"{emoji} · {tenant_full} · "
            f"{unit_part}{unit['property_name']} · "
            f"${payment['amount']:,.0f}{tag}"
        )

        nsf_note    = "  ⚠️ REVERSED / NSF" if payment['is_nsf'] else ""
        intended    = payment.get('intended_month')
        bal_display = max(0.0, running_balance)
        has_credit  = running_balance < 0

        desc = [
            f"Payment {payment_num} of {total_payments} in {month_label}",
            f"Date:         {pay_display}",
            f"Method:       {payment['description']}{nsf_note}",
            f"Amount:       ${payment['amount']:,.2f}",
        ]
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

    # ── Event find / upsert / delete ─────────────────────────────────────────
    # All Google API calls below use _gcal_execute() for retry on rate limits.

    def _find_event(
        self, calendar_id: str, occupancy_id: str, month: str, event_type: str,
    ) -> Optional[str]:
        # Events are tagged with the RAW occupancy_id from AppFolio
        # (unit["occupancy_id"]), never the owner-scoped soid.  Strip the
        # "@owner_id" suffix so the search matches the tag on the event.
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
