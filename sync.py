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

import os, re, json, time, logging, requests
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from local_config import get_config, load_json_config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APPFOLIO_DB_NAME       = get_config("APPFOLIO_DB_NAME")
APPFOLIO_CLIENT_ID     = get_config("APPFOLIO_CLIENT_ID")
APPFOLIO_CLIENT_SECRET = get_config("APPFOLIO_CLIENT_SECRET")
GOOGLE_SA_INFO         = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SCOPES          = ["https://www.googleapis.com/auth/calendar"]

LATE_GRACE_DAYS             = int(get_config("LATE_GRACE_DAYS", 5))
RENT_DUE_DAY                = int(get_config("RENT_DUE_DAY", 1))
PM_EMAIL                    = str(get_config("PM_EMAIL", ""))
DEFAULT_LEASE_MONTHS        = int(get_config("DEFAULT_LEASE_MONTHS", 12))
FORCE_REFRESH               = str(get_config("FORCE_REFRESH", "")).lower() == "true"
COMMITMENT_LOOKAHEAD_MONTHS = int(get_config("COMMITMENT_LOOKAHEAD_MONTHS", 3))
TIMEZONE                    = str(get_config("TIMEZONE", "America/Chicago"))

STATE_FILE       = Path("state.json")
CALENDAR_PREFIX  = "OKPM"
AF_API_DELAY_SEC = 2.0

_AF_BASE    = (f"https://{APPFOLIO_CLIENT_ID}:{APPFOLIO_CLIENT_SECRET}"
               f"@{APPFOLIO_DB_NAME}.appfolio.com/api/v2/reports")
_AF_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Divider between PM-editable notes and auto-generated section in commitments
COMMITMENT_DIVIDER = "─" * 16 + " AUTO-SYNCED — do not edit below " + "─" * 16

# Google Calendar color IDs
COLOR_PAID       = "2"   # sage green
COLOR_PREPAID    = "4"   # flamingo pink
COLOR_PARTIAL    = "5"   # banana yellow
COLOR_UNPAID     = "11"  # tomato red
COLOR_LATE       = "11"  # tomato red
COLOR_COMMITMENT = "6"   # tangerine — distinct colour for commitment events

GCAL_RETRY_ATTEMPTS = 3
GCAL_RETRY_BASE_DELAY = 5   # seconds — doubled on each retry


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

# ---------------------------------------------------------------------------
# Status system
# ---------------------------------------------------------------------------
STATUS_PAID    = "✅ Paid"
STATUS_PREPAID = "🩷 Prepaid"
STATUS_PARTIAL = "🟡 Partial"
STATUS_UNPAID  = "🔴 Unpaid"
STATUS_LATE    = "🔴 Late"
STATUS_OVERDUE = "⚠️ Overdue"   # a promised payment whose date has passed unpaid


def classify_status(rent: float, past_due: float) -> str:
    if past_due < 0:      return STATUS_PREPAID
    elif past_due == 0:   return STATUS_PAID
    elif past_due < rent: return STATUS_PARTIAL
    else:                 return STATUS_UNPAID


def color_for_status(status: str) -> str:
    return {
        STATUS_PAID:    COLOR_PAID,
        STATUS_PREPAID: COLOR_PREPAID,
        STATUS_PARTIAL: COLOR_PARTIAL,
        STATUS_UNPAID:  COLOR_UNPAID,
        STATUS_LATE:    COLOR_LATE,
        STATUS_OVERDUE: COLOR_COMMITMENT,   # tangerine
    }.get(status, COLOR_UNPAID)


def emoji_for_status(status: str) -> str:
    return {
        STATUS_PAID:    "✅",
        STATUS_PREPAID: "🩷",
        STATUS_PARTIAL: "🟡",
        STATUS_UNPAID:  "🔴",
        STATUS_LATE:    "🔴",
        STATUS_OVERDUE: "⚠️",
    }.get(status, "🔴")


# ---------------------------------------------------------------------------
# AppFolio client  (unchanged)
# ---------------------------------------------------------------------------
class AppFolioClient:

    def _post_report(self, report: str, payload: dict = None) -> list[dict]:
        url, results = f"{_AF_BASE}/{report}.json", []
        while url:
            r = requests.post(url, headers=_AF_HEADERS, json=(payload or {}), timeout=30)
            if r.status_code == 429:
                log.warning("AppFolio rate limit — waiting 60s"); time.sleep(60); continue
            r.raise_for_status()
            body = r.json()
            results.extend(body.get("results", []))
            url = body.get("next_page_url"); payload = None
        time.sleep(AF_API_DELAY_SEC)
        return results

    def get_rent_roll(self)       -> list[dict]: return self._post_report("rent_roll")
    def get_owner_directory(self) -> list[dict]: return self._post_report("owner_directory")
    def get_tenant_directory(self)-> list[dict]: return self._post_report("tenant_directory")
    def get_tenant_ledger_month(self, from_date: str, to_date: str) -> list[dict]:
        return self._post_report("tenant_ledger", {"from_date": from_date, "to_date": to_date})


# ---------------------------------------------------------------------------
# Data helpers  (unchanged)
# ---------------------------------------------------------------------------
_MONTH_NAMES = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def normalize_tenant_name(name: str) -> str:
    """'Last, First' → 'First Last'"""
    name = (name or "").strip()
    if "," in name:
        parts = name.split(",", 1)
        return f"{parts[1].strip()} {parts[0].strip()}"
    return name


def detect_intended_month(desc: str, payment_date: str) -> Optional[tuple]:
    match = re.search(r"\b(" + "|".join(_MONTH_NAMES) + r")\s+rent\b", desc.lower())
    if not match: return None
    intended = _MONTH_NAMES[match.group(1)]
    try: pay = date.fromisoformat(payment_date)
    except ValueError: return None
    if intended == pay.month: return None
    year = pay.year - (1 if intended > pay.month + 1 else 0)
    return (year, intended)


def _shorten_desc(desc: str) -> str:
    desc = re.sub(r'ACH Payment \(Reference (#[\w-]+)\)', r'ACH (\1)', desc)
    desc = re.sub(r'Credit Card Payment \(Reference (#[\w-]+)\)', r'Credit Card (\1)', desc)
    desc = re.sub(r'Payment \(Reference #(\w+)\)\s*', r'\1 - ', desc)
    return desc[:80].strip(" -")


def build_owner_property_map(owners: list[dict]) -> dict:
    """Maps property_id → list of owners (supports co-ownership)."""
    m: dict[int, list[dict]] = {}
    for o in owners:
        for pid in (o.get("properties_owned_i_ds") or "").split(","):
            if pid.strip().isdigit():
                m.setdefault(int(pid.strip()), []).append(o)
    return m


def build_tenant_info_map(tenants: list[dict]) -> dict:
    m = {}
    for t in tenants:
        if t.get("primary_tenant") != "Yes": continue
        oid = t.get("occupancy_id")
        if not oid: continue
        raw_phone  = (t.get("phone_numbers") or "").strip()
        phone      = raw_phone.replace("Phone:","").replace("Mobile:","").replace("Fax:","").strip()
        fee_type   = (t.get("late_fee_type") or "").strip()
        fee_base   = float(t.get("late_fee_base_amount") or 0)
        fee_daily  = float(t.get("late_fee_daily_amount") or 0)
        grace_days = int(t.get("rent_grace_days") or LATE_GRACE_DAYS)
        if fee_type == "Flat Fee":
            fee_desc = f"Flat ${fee_base:,.2f} after {grace_days} days"
        elif fee_daily > 0:
            fee_desc = f"${fee_base:,.2f} + ${fee_daily:,.2f}/day after {grace_days} days"
        elif fee_base > 0:
            fee_desc = f"${fee_base:,.2f} after {grace_days} days"
        else:
            fee_desc = f"No late fee ({grace_days} days grace)"
        m[int(oid)] = {"phone": phone or "N/A", "late_fee_desc": fee_desc, "grace_days": grace_days}
    return m


def build_payment_map(ledger_rows: list[dict]) -> dict:
    payments = {}
    for row in ledger_rows:
        try: amount = float(row.get("credit") or 0)
        except: continue
        if amount <= 0: continue
        payer    = normalize_tenant_name(row.get("payer") or "Unknown")
        desc     = (row.get("description") or "").strip()
        raw_date = row.get("date", "")
        is_nsf   = "nsf" in desc.lower() or "reversed" in desc.lower()
        payments.setdefault(payer, []).append({
            "date":           raw_date,
            "amount":         amount,
            "description":    _shorten_desc(desc),
            "is_nsf":         is_nsf,
            "intended_month": detect_intended_month(desc, raw_date),
        })
    return payments


def compute_running_balances(sorted_payments: list[dict], current_past_due: float) -> list[float]:
    balances = []
    for i, p in enumerate(sorted_payments):
        subsequent = sum(pp["amount"] for pp in sorted_payments[i+1:] if not pp["is_nsf"])
        balances.append(current_past_due + subsequent)
    return balances


def format_address(row: dict) -> str:
    return ", ".join(p for p in [
        row.get("property_street",""), row.get("property_city",""),
        row.get("property_state",""), row.get("property_zip","") or "",
    ] if p)


def unit_label(row: dict) -> str:
    raw = (row.get("unit") or "").strip()
    if not raw:
        return ""
    # AppFolio is inconsistent: some properties store "Unit 2", others store a
    # bare "2". Normalize so every label reads "Unit X" (and never "Unit Unit").
    return raw if raw.lower().startswith("unit") else f"Unit {raw}"


def owner_display_name(owner: dict) -> str:
    name = (owner.get("name") or "").strip()
    if name: return name
    return f"{(owner.get('first_name') or '').strip()} {(owner.get('last_name') or '').strip()}".strip() or "Unknown Owner"


# ---------------------------------------------------------------------------
# Google Calendar manager
# ---------------------------------------------------------------------------
class GoogleCalendarManager:

    def __init__(self):
        creds = service_account.Credentials.from_service_account_info(
            GOOGLE_SA_INFO, scopes=GOOGLE_SCOPES)
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
        body["start"]["date"] = anchor_date
        body["end"]["date"]   = anchor_date
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
        new_body["end"]["date"]   = live_date

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
        ev["end"]   = {"date": canonical_date}
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
            "end":         {"date": anchor_date},
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
    ) -> dict:
        emoji        = emoji_for_status(event_status)
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
            "end":         {"date": event_date.isoformat()},
            "colorId":     color_for_status(event_status),
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
        pay_date   = payment["date"]
        pay_status = classify_status(unit['rent'], running_balance)
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
            "end":         {"date": pay_date},
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
        tenant_short = unit['tenant'].split(",")[0].strip()
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
            "end":         {"date": due_date.isoformat()},
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


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------
class StateManager:
    """
    Per occupancy+month  (existing keys unchanged):
      status, past_due,
      status_event_id, status_event_date,
      late_event_id,
      payment_event_ids,
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
        },
        ...   # one entry per split (copy-pasted events)
      ]
    """

    def __init__(self):
        self.path = STATE_FILE
        self.data: dict = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        # Ensure commitment registry exists
        if "_commitments" not in self.data:
            self.data["_commitments"] = {}

    def _key(self, oid: str, month: str) -> str:
        return f"{oid}_{month}"

    def get(self, oid: str, month: str) -> Optional[dict]:
        # NOTE: no bare-oid fallback. A unit co-owned by several owners is
        # processed once per owner with a distinct owner-scoped key
        # (f"{oid}@{owner_id}"). Falling back to the bare-oid entry would make
        # the 2nd+ owner inherit the 1st owner's sync state and skip creating
        # events on their calendar. Each calendar's events are still found and
        # de-duplicated via _find_status_event/_find_event (which search by the
        # event's extendedProperties on that specific calendar), so dropping the
        # fallback never creates duplicates — it only lets each calendar build
        # its own events independently.
        return self.data.get(self._key(oid, month))

    def set(self, oid: str, month: str, entry: dict):
        entry["last_updated"] = datetime.utcnow().isoformat()
        self.data[self._key(oid, month)] = entry

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

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


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------
class SyncOrchestrator:

    def __init__(self):
        self.af    = AppFolioClient()
        self.gcal  = GoogleCalendarManager()
        self.state = StateManager()

    # ── Top-level run  (unchanged) ────────────────────────────────────────────

    def run(self):
        log.info("=== OKPM sync starting ===")
        today      = datetime.now(ZoneInfo(TIMEZONE)).date()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)
        log.info(f"  Timezone: {TIMEZONE}, local date: {today}")

        log.info("Fetching rent_roll...")
        rent_roll = self.af.get_rent_roll()
        log.info("Fetching owner_directory...")
        owners = self.af.get_owner_directory()
        log.info("Fetching tenant_directory...")
        tenants = self.af.get_tenant_directory()
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

        for owner_id, rows_and_owners in owner_rows.items():
            owner       = rows_and_owners[0][1]
            owner_name  = owner_display_name(owner)
            owner_email = (owner.get("email") or "").strip()
            log.info(f"Owner: {owner_name} ({len(rows_and_owners)} units)")
            calendar_id = self.gcal.get_or_create_calendar(owner_name)
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

        self.state.save()
        log.info("=== Sync complete ===")

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

    def _make_unit(self, row: dict, tenant_info: dict, payment_map: dict) -> dict:
        oid      = str(row["occupancy_id"])
        rent     = float(row.get("rent", 0) or 0)
        past_due = float(row.get("past_due", 0) or 0)
        info     = tenant_info.get(int(oid), {})
        t_norm   = normalize_tenant_name(row.get("tenant", ""))
        payments = payment_map.get(t_norm, [])
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

        try:
            lease_end = date.fromisoformat(unit["lease_to"])
        except ValueError:
            m = due_date.month + DEFAULT_LEASE_MONTHS
            y = due_date.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            lease_end = date(y, m, 1)

        sorted_payments = sorted(unit["payments"], key=lambda p: (p["date"], -p["amount"]))

        # ── Filter payments to current month ───────────────────────────────
        # The AppFolio tenant_ledger API may return transactions outside the
        # requested date range (similar to how it ignores occupancy_id
        # filters).  Payments from previous months would place the status
        # event on a past date, making it vanish from the current month's
        # calendar view.  Keep only payments whose date falls in this_month
        # or later.
        sorted_payments = [
            p for p in sorted_payments
            if p["date"][:7] >= this_month
        ]
        # Update amount_paid to reflect filtered payments only
        unit["amount_paid"] = sum(
            p["amount"] for p in sorted_payments if not p["is_nsf"])

        balances        = compute_running_balances(sorted_payments, past_due)
        prior           = self.state.get(soid, this_month)

        # Distrust state written for a DIFFERENT calendar.  Earlier runs (before
        # co-ownership state-scoping was fixed) could persist a soid entry whose
        # event IDs actually live on another co-owner's calendar — e.g. Cloutier
        # inheriting Palmer's status_event_id for a shared unit.  If the stored
        # calendar_id doesn't match this calendar (or is absent, i.e. an entry
        # written before tagging existed), drop it so the event is re-resolved
        # against THIS calendar via _find_status_event, which de-dupes by event
        # properties and therefore never creates duplicates.
        if prior and prior.get("calendar_id") != calendar_id:
            prior = None

        # ── Load commitment state ─────────────────────────────────────────────
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
            event_status      = classify_status(rent, balances[0])
        else:
            status_event_date = due_date
            first_pay         = None
            event_status      = status

        # ── Kickstart suppression ─────────────────────────────────────────────
        # When a commitment covers this month and no payments exist yet,
        # we skip creating/keeping a status event on the 1st.
        # Only suppress if the prior entry was a frozen placeholder (rent_event_id),
        # not an already-established status event.
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
            # month — by an earlier version that snapped dragged events back, or
            # as duplicates.  The commitment now represents the month, so none
            # should remain.  _find_event auto-dedupes, so this also collapses
            # duplicate copies.  Never delete the commitment event itself.
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
        # If the PM drags the current-month status event to a FUTURE date, treat
        # it as a promise-to-pay.  Convert THAT SAME event in-place into a
        # commitment — no second event, no snapping the original back to the 1st
        # — and suppress this month's status event.  The single event the PM
        # dragged simply becomes the commitment at the promised date.
        if (prior and prior.get("status_event_id")
                and not FORCE_REFRESH
                and not suppress_kickstart
                and this_month not in commitment_months):
            _drag_live  = self.gcal.get_event_start_date(
                calendar_id, prior["status_event_id"])
            _drag_canon = prior.get("status_event_date", due_date.isoformat())
            if (_drag_live
                    and _drag_live != _drag_canon
                    and _drag_live > today.isoformat()):
                ev_id = prior["status_event_id"]
                self.gcal.convert_to_commitment(
                    calendar_id, ev_id, unit, _drag_live, "status",
                    max(0.0, past_due), source_status=status)
                self.state.add_commitment(soid, {
                    "event_id":          ev_id,
                    "anchor_date":       _drag_live,
                    "source_type":       "status",
                    "origin_month":      this_month,
                    "calendar_id":       calendar_id,
                    "covers_rent_month": this_month,
                })
                commitment_months.add(this_month)
                suppress_kickstart  = True   # don't re-create a status event
                # The converted event WAS the status event; forget its id so the
                # status-build and locked-event checks below leave it alone.
                prior = {**prior, "status_event_id": None}
                log.info(
                    f"  {oid}: status event dragged to {_drag_live} → "
                    f"converted in place to commitment (month suppressed)")

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
            )
            existing_id = (
                prior_status_id or
                self.gcal._find_status_event(calendar_id, oid, this_month)
            )
            status_event_id = self.gcal._update_or_create(
                calendar_id, existing_id, body)
            log.info(f"  Status event {oid}: {event_status} on {status_event_date}")
        else:
            status_event_id = prior_status_id
            log.info(f"  No change for {oid} — skipping status event")

        # ── Additional payment events ─────────────────────────────────────────
        if FORCE_REFRESH or data_changed or new_payments:
            payment_event_ids = self._sync_additional_payments(
                unit, calendar_id, this_month,
                sorted_payments[1:], balances[1:], prior,
            )
        else:
            payment_event_ids = prior.get("payment_event_ids", []) if prior else []

        # ── Detect-and-revert locked events ──────────────────────────────────
        # Only needed when we did NOT just write the event this run; skipped
        # for commitment-suppressed months (no real status event to verify).
        if not suppress_kickstart and not (FORCE_REFRESH or date_changed or data_changed):
            self._verify_locked_events(
                oid, calendar_id, prior,
                status_event_id, status_event_date, sorted_payments,
                unit, today, this_month, past_due, status, commitment_months,
            )

        # ── Retire any legacy "today" / late preview event ────────────────────
        # The daily today-marker dashboard has been removed.  Delete any marker
        # left behind by a previous version so it doesn't linger on the calendar.
        # Promises are now started by dragging the 1st-of-month status event (or a
        # payment / future placeholder) forward.  Existing "late"-sourced
        # commitments already on the calendar are untouched and stay managed by
        # _process_commitments below.
        prior_late_id = prior.get("late_event_id") if prior else None
        if prior_late_id:
            self.gcal.delete_event(calendar_id, prior_late_id)
            log.info(f"  {oid}: removed retired today/late preview event")
        late_event_id = None

        # ── Process all commitments for this unit ─────────────────────────────
        # Always runs — including during FORCE_REFRESH — so commitment events
        # are preserved and recreated even after a nuke-and-rebuild.
        # Trigger on the owner-scoped key OR a legacy bare-oid key (orphans left
        # by an earlier key-mismatch bug); the latter get re-tracked under soid
        # from the calendar's live events on this run.
        if self.state.get_commitments(soid) or self.state.get_commitments(oid):
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
        })

        # ── B. Future months ──────────────────────────────────────────────────
        future_unit = {**unit, "past_due": 0.0, "amount_paid": 0.0, "payments": []}
        has_credit  = past_due < 0

        # During FORCE_REFRESH: purge ALL events for this unit that belong
        # to FUTURE months (anything with okpm_month > this_month).  This
        # guarantees a clean slate — no orphaned / corrupted / wrong-date /
        # wrong-type events survive.  Current-month events (status, payment,
        # late) are kept because they were just written above.
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
                # Preserve commitment events — they represent PM work
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
            # Same cross-calendar distrust as current-month state (see above):
            # drop placeholders whose stored calendar_id doesn't match, so each
            # calendar rebuilds its own future placeholders via upsert_event.
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
                # Skip placeholder creation/update.
                # The existing placeholder (if any) stays visible until this
                # month becomes current, at which point suppress_kickstart
                # deletes it.  (Double-display is intentional.)
                continue

            # ── Scan first COMMITMENT_LOOKAHEAD_MONTHS for moved kickstarts ──
            # Skip during FORCE_REFRESH to avoid extra API reads that cause
            # rate-limit issues.  Commitment detection runs on normal polls only.
            if prior_f and i < COMMITMENT_LOOKAHEAD_MONTHS and not FORCE_REFRESH:
                placeholder_id = prior_f.get("rent_event_id")
                if placeholder_id and not prior_f.get("is_commitment"):
                    live_date = self.gcal.get_event_start_date(
                        calendar_id, placeholder_id)
                    expected  = fdue.isoformat()
                    if live_date and live_date != expected:
                        if live_date > today.isoformat():
                            # PM moved this kickstart → commitment!
                            self.gcal.convert_to_commitment(
                                calendar_id, placeholder_id, unit,
                                live_date, "kickstart", max(0.0, past_due),
                                source_status=STATUS_UNPAID,
                            )
                            self.state.add_commitment(soid, {
                                "event_id":           placeholder_id,
                                "anchor_date":        live_date,
                                "source_type":        "kickstart",
                                "origin_month":       fmonth,
                                "calendar_id":       calendar_id,
                    "covers_rent_month":  fmonth,
                            })
                            self.state.set(soid, fmonth, {
                                **prior_f, "is_commitment": True,
                            })
                            log.info(
                                f"  {oid}: kickstart for {fmonth} moved "
                                f"to {live_date} → commitment registered")
                            continue
                        else:
                            log.warning(
                                f"  {oid}: kickstart for {fmonth} moved to "
                                f"{live_date} (past/today) — ignoring, not a future commitment")
                    elif live_date is None:
                        log.warning(
                            f"  {oid}: kickstart {placeholder_id} for {fmonth} — "
                            f"event not found in Google (deleted?)")
                        # Clear stale ID so the frozen-placeholder check below
                        # doesn't block recreation of a fresh placeholder.
                        prior_f = {**prior_f, "rent_event_id": None}
                        # Persist the clear under BOTH the owner-scoped key and
                        # the bare-oid key.  Otherwise the stale bare-oid entry
                        # keeps shadowing via StateManager.get()'s fallback and
                        # the warning repeats on every run.
                        self.state.set(soid, fmonth, prior_f)
                        bare_oid = soid.split("@")[0]
                        if bare_oid != soid:
                            bare_prior = self.state.data.get(
                                self.state._key(bare_oid, fmonth))
                            if bare_prior:
                                self.state.set(bare_oid, fmonth, {
                                    **bare_prior, "rent_event_id": None})

            # ── Normal frozen-placeholder logic ─────────────────────────────────
            # During FORCE_REFRESH we rewrite ALL placeholders (nuke & rebuild),
            # which fixes stale/wrong status from prior runs.  During normal
            # polls, frozen placeholders are skipped as before.
            if not FORCE_REFRESH and prior_f and prior_f.get("rent_event_id") and not (is_next and has_credit):
                continue

            if is_next and has_credit:
                projected  = rent + past_due         # past_due is negative
                # On kickstart placeholders, show green (✅ Paid) even if the
                # credit exceeds one month's rent.  Pink 🩷 Prepaid is reserved
                # for current-month logging events only.
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
    ) -> list[str]:
        """Sync payment events for idx 1+. Payment 0 is absorbed into status event."""
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
                f"  Payment {i+2}/{total} for {unit['occupancy_id']} "
                f"on {payment['date']}")

        return event_ids

    # ── Locked-event revert ───────────────────────────────────────────────────

    def _verify_locked_events(
        self,
        oid: str,
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
        event back.  Otherwise just revert.
        """
        if not prior or not status_event_id:
            return

        soid = oid  # oid is already owner-scoped when passed from _sync_unit
        # ── Status event ──────────────────────────────────────────────────
        if prior.get("status_event_id"):
            live = self.gcal.get_event_start_date(calendar_id, status_event_id)
            canon = canonical_status_date.isoformat()
            if live and live != canon:
                if live > today.isoformat():
                    if this_month not in commitment_months:
                        # PM dragged status event to future → register commitment
                        new_body = self.gcal._build_commitment_event(
                            unit, live, "status", max(0.0, past_due),
                            source_status=status)
                        created = _gcal_execute(self.gcal.service.events().insert(
                            calendarId=calendar_id, body=new_body))
                        self.state.add_commitment(soid, {
                            "event_id":          created["id"],
                            "anchor_date":       live,
                            "source_type":       "status",
                            "origin_month":      this_month,
                            "calendar_id":       calendar_id,
                    "covers_rent_month": this_month,
                        })
                        commitment_months.add(this_month)
                        log.info(
                            f"  {oid}: status event dragged to {live} "
                            f"→ commitment registered")
                    # Snap back silently (commitment already covers this drag)
                    ev = self.gcal.get_event(calendar_id, status_event_id)
                    if ev:
                        ev["start"] = {"date": canon}
                        ev["end"]   = {"date": canon}
                        try:
                            _gcal_execute(self.gcal.service.events().update(
                                calendarId=calendar_id,
                                eventId=status_event_id, body=ev))
                        except HttpError as e:
                            log.error(f"  Failed to snap back {status_event_id}: {e}")
                else:
                    # Dragged to past — warn and use standard revert
                    self.gcal.revert_event_to_date(
                        calendar_id, status_event_id, canon)

        # ── Payment events ────────────────────────────────────────────────
        additional_payments = sorted_payments[1:]   # [0] absorbed into status
        for i, event_id in enumerate(prior.get("payment_event_ids", [])):
            if i < len(additional_payments):
                pay_canon = additional_payments[i]["date"]
                live = self.gcal.get_event_start_date(calendar_id, event_id)
                if live and live != pay_canon:
                    if (live > today.isoformat()
                            and this_month not in commitment_months):
                        new_body = self.gcal._build_commitment_event(
                            unit, live, "payment", max(0.0, past_due),
                            source_status=status)
                        created = _gcal_execute(
                            self.gcal.service.events().insert(
                                calendarId=calendar_id, body=new_body))
                        self.state.add_commitment(soid, {
                            "event_id":          created["id"],
                            "anchor_date":       live,
                            "source_type":       "payment",
                            "origin_month":      this_month,
                            "calendar_id":       calendar_id,
                    "covers_rent_month": this_month,
                        })
                        commitment_months.add(this_month)
                        log.info(
                            f"  {oid}: payment event dragged to {live} "
                            f"→ commitment registered")
                    # Snap payment event back
                    self.gcal.revert_event_to_date(
                        calendar_id, event_id, pay_canon)

    # ── Commitment lifecycle ──────────────────────────────────────────────────

    def _process_commitments(
        self,
        soid: str,
        calendar_id: str,
        unit: dict,
        today: date,
        has_known_or_new: bool = False,
    ):
        """
        For each tracked commitment:
          1. Discover new copies (PM copy-pasted for split payment plans).
          2. Resolve (delete every promise) if account balance ≤ 0.
          3. Update the auto section, preserving PM notes above the divider.
             Display recomputes from the live date + balance: ⚠️ Overdue once the
             promised date has passed unpaid, otherwise 🔴 / 🟡.  No auto-expire.
             Also picks up re-drags (PM moved the commitment again).
          4. Safe delete (≥1-promise rule): a deleted promise sticks only while
             another promise remains; deleting the LAST promise recreates one.
          Kickstart placeholders keep their own recreate + drag-back-to-1st
          behaviour and are exempt from the ≥1-promise rule.

        Optimisation: skips the Google list call entirely when no commitments
        are known and none were registered this run.
        """
        if not has_known_or_new:
            return

        # State is keyed by the owner-scoped soid (e.g. "96@owner42"); Google
        # Calendar events are tagged with the BARE occupancy_id.  Use each key in
        # its own domain — this was the root of commitments not refreshing
        # (titles, suppression) for owner-scoped units.
        bare_oid = soid.split("@")[0] if "@" in soid else soid
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
                    "covers_rent_month":  (
                        anchor[:7] if (src == "late" and anchor[:7] > today_month)
                        else anchor[:7] if src == "kickstart"
                        else today_month if src in ("status", "payment")
                        else None
                    ),
                }
                self.state.add_commitment(soid, new_c)
                log.info(f"  {bare_oid}: discovered new split commitment on {anchor}")

        # Reload after potential additions
        commitments = self.state.get_commitments(soid)
        if not commitments:
            return

        past_due    = unit["past_due"]
        rent        = unit["rent"]
        today_str   = today.isoformat()
        today_month = today.strftime("%Y-%m")
        surviving        = []   # commitments that persist into the next run
        missing_promises = []   # promise commitments whose event the PM deleted

        def _is_promise(src: str) -> bool:
            # Promise-to-pay commitments (PM-created).  Kickstart placeholders are
            # a separate mechanism and are NOT subject to the ≥1-promise rule.
            return src in ("status", "payment", "late")

        for c in commitments:
            # Skip commitments that belong to a different calendar.
            # calendar_id is stored in the entry when the commitment is created;
            # entries without it (old format) are treated as belonging to any
            # calendar (backward compat — will be fixed on first update).
            c_cal = c.get("calendar_id")
            if c_cal and c_cal != calendar_id:
                surviving.append(c)  # keep it; belongs to another owner's calendar
                continue

            event_id          = c["event_id"]
            anchor_date       = c["anchor_date"]
            source_type       = c.get("source_type", "late")
            covers_rent_month = c.get("covers_rent_month")

            # ── Resolve if fully paid ─────────────────────────────────────────
            # Full payment (or a credit) is the ONLY thing that clears promises:
            # every commitment for the unit is removed.  (Eviction handling is a
            # separate track for another day.)
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
                    # A promise delete is honoured ONLY if another promise still
                    # remains (PM rearranging an installment plan).  Deleting the
                    # LAST promise is treated as a slip — it is recreated after the
                    # loop (see the ≥1-promise rule).  Defer the decision.
                    missing_promises.append(c)
                    continue
                # Kickstart placeholder: recreate as before (auto-managed).
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

            # ── Kickstart drag-back to its origin 1st → revert to placeholder ──
            # (Only kickstart placeholders un-commit this way.  Promises never
            #  un-commit — they clear only on full payment.)
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

            # ── Compute displayed outstanding ─────────────────────────────────
            if covers_rent_month and covers_rent_month > today_month:
                # Pre-load future month's rent before it accrues in AppFolio
                outstanding = past_due + rent
            else:
                # AppFolio's past_due already includes this month
                outstanding = past_due

            # ── Display status: overdue ⚠️  vs. live 🔴 / 🟡 ───────────────────
            # A promise whose live date has already passed and is still unpaid
            # shows ⚠️ Overdue (no auto-expire — the marker stays put).  Drag it
            # onto today or a future date and it reverts to 🔴 (owes a full month
            # or more) or 🟡 (partial) — recomputed every run from the live date
            # and the current AppFolio balance.  Kickstart placeholders keep their
            # own colour logic (source_status left blank).
            display_status = ""
            if _is_promise(source_type):
                display_status = (
                    STATUS_OVERDUE if live_date < today_str
                    else classify_status(rent, past_due)
                )

            # ── Update event (preserves PM notes, picks up re-drags) ──────────
            # Refresh event_id — it may have changed if a kickstart was just
            # recreated above (old variable holds the deleted event's ID).
            event_id = c["event_id"]
            new_live_anchor = self.gcal.update_commitment_event(
                calendar_id, event_id, ev_body,
                unit, anchor_date, source_type, outstanding,
                source_status=display_status,
            )

            # Persist any changes: re-drag updates anchor, and covers_rent_month
            updated_c = {**c, "anchor_date": new_live_anchor}
            if source_type == "late":
                updated_c["covers_rent_month"] = (
                    new_live_anchor[:7]
                    if new_live_anchor[:7] > today_month
                    else None
                )
            elif source_type in ("status", "payment"):
                # A dragged status/payment event always represents its origin
                # month's rent, so it must keep suppressing that month's status
                # event no matter where it's dragged.  Repairs entries left with
                # covers_rent_month=None by the earlier key-mismatch bug.
                updated_c["covers_rent_month"] = (
                    c.get("covers_rent_month")
                    or c.get("origin_month")
                    or today_month
                )
            # For kickstart commitments, covers_rent_month stays = origin_month

            surviving.append(updated_c)

        # ── ≥1-promise rule (mistake-proofing) ─────────────────────────────────
        # A unit being tracked by promises must always keep at least one promise
        # on the calendar until the balance is paid in full.  Deleting a promise
        # is honoured only while another promise survives (installment
        # rearrangement, above).  If the PM deleted the LAST promise, treat it as
        # an accidental removal and recreate one — the most recent promised date —
        # so tracking is never lost to a stray delete.
        if (past_due > 0 and missing_promises
                and not any(_is_promise(c.get("source_type", "late"))
                            for c in surviving)):
            c                 = max(missing_promises,
                                    key=lambda x: x.get("anchor_date", ""))
            anchor_date       = c["anchor_date"]
            source_type       = c.get("source_type", "late")
            covers_rent_month = c.get("covers_rent_month")
            outstanding = past_due + (
                rent if (covers_rent_month and covers_rent_month > today_month)
                else 0
            )
            disp = (STATUS_OVERDUE if anchor_date < today_str
                    else classify_status(rent, past_due))
            new_body = self.gcal._build_commitment_event(
                unit, anchor_date, source_type, outstanding,
                pm_notes=(
                    "PROMISED: [fill in, e.g. $500 or 'full balance']\n"
                    "NOTES:    [optional context]"
                ),
                source_status=disp,
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    SyncOrchestrator().run()