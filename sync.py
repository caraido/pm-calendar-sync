"""
OKPM AppFolio → Google Calendar Sync
======================================
Polls AppFolio Plus Reports API (v2) and maintains per-owner Google Calendars
with color-coded rent status events.

API calls per run: exactly 2 (rent_roll + owner_directory).
Calendars are owned by the OKPM service account; owners subscribe read-only.

Field names verified against live AppFolio API 2026-05-17.
"""

import os
import json
import time
import logging
import requests
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — all from environment / GitHub secrets
# ---------------------------------------------------------------------------
APPFOLIO_DB_NAME       = os.environ["APPFOLIO_DB_NAME"]          # "openkey"
APPFOLIO_CLIENT_ID     = os.environ["APPFOLIO_CLIENT_ID"]
APPFOLIO_CLIENT_SECRET = os.environ["APPFOLIO_CLIENT_SECRET"]

GOOGLE_SA_JSON  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]      # full JSON string
GOOGLE_SCOPES   = ["https://www.googleapis.com/auth/calendar"]

LATE_GRACE_DAYS      = int(os.environ.get("LATE_GRACE_DAYS", 5))
RENT_DUE_DAY         = int(os.environ.get("RENT_DUE_DAY", 1))
PM_EMAIL             = os.environ.get("PM_EMAIL", "")
# Months ahead to create future events when lease_to is null (month-to-month tenants)
DEFAULT_LEASE_MONTHS = int(os.environ.get("DEFAULT_LEASE_MONTHS", 12))
# Set to "true" to force-update all current-month events regardless of delta
# Use once to fix formatting, then remove/set back to false
FORCE_REFRESH        = os.environ.get("FORCE_REFRESH", "").lower() == "true"
STATE_FILE       = Path("state.json")
CALENDAR_PREFIX  = "OKPM"                                         # → "OKPM · Ryan Palmer Portfolio"
AF_API_DELAY_SEC = 2.0   # pause between AppFolio API calls to avoid 429

# AppFolio v2 Reports API base — credentials embedded in URL per their spec
_AF_BASE = (
    f"https://{APPFOLIO_CLIENT_ID}:{APPFOLIO_CLIENT_SECRET}"
    f"@{APPFOLIO_DB_NAME}.appfolio.com/api/v2/reports"
)
_AF_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Google Calendar color IDs
COLOR_PAID    = "2"   # sage green
COLOR_PARTIAL = "5"   # banana yellow
COLOR_UNPAID  = "11"  # tomato red
COLOR_LATE    = "6"   # tangerine orange

# ---------------------------------------------------------------------------
# Payment status helpers
# ---------------------------------------------------------------------------
STATUS_PAID    = "✅ Paid"
STATUS_PREPAID = "💚 Prepaid"
STATUS_PARTIAL = "🟡 Partial"
STATUS_UNPAID  = "🔴 Unpaid"
STATUS_LATE    = "⚠️ Late"


def classify_status(rent: float, past_due: float) -> str:
    """
    Classify payment status using rent_roll.past_due directly.

    past_due < 0          → overpaid — credit balance toward next month
    past_due = 0          → fully paid and current
    0 < past_due < rent   → partial payment received
    past_due >= rent      → no payment received this month
    """
    if past_due < 0:
        return STATUS_PREPAID
    elif past_due == 0:
        return STATUS_PAID
    elif past_due < rent:
        return STATUS_PARTIAL
    else:
        return STATUS_UNPAID


def color_for_status(status: str) -> str:
    return {
        STATUS_PAID:    COLOR_PAID,
        STATUS_PREPAID: COLOR_PAID,
        STATUS_PARTIAL: COLOR_PARTIAL,
        STATUS_UNPAID:  COLOR_UNPAID,
        STATUS_LATE:    COLOR_LATE,
    }.get(status, COLOR_UNPAID)


def emoji_for_status(status: str) -> str:
    """Return emoji only — text label is redundant alongside color coding."""
    return {
        STATUS_PAID:    "✅",
        STATUS_PREPAID: "💚",
        STATUS_PARTIAL: "🟡",
        STATUS_UNPAID:  "🔴",
        STATUS_LATE:    "⚠️",
    }.get(status, "🔴")


# ---------------------------------------------------------------------------
# AppFolio client  (v2 Reports API — verified field names)
# ---------------------------------------------------------------------------
class AppFolioClient:
    """
    Two reports only:
      POST /api/v2/reports/rent_roll.json       → all current leases
      POST /api/v2/reports/owner_directory.json → all owners with property IDs

    Pagination: AppFolio v2 returns {"results": [...], "next_page_url": "..."}
    """

    def _post_report(self, report: str, payload: dict = None) -> list[dict]:
        """POST a report and follow next_page_url pagination."""
        url = f"{_AF_BASE}/{report}.json"
        results = []
        while url:
            r = requests.post(
                url, headers=_AF_HEADERS,
                json=(payload or {}), timeout=30
            )
            if r.status_code == 429:
                log.warning("AppFolio rate limit hit — waiting 60s")
                time.sleep(60)
                continue
            r.raise_for_status()
            body = r.json()
            results.extend(body.get("results", []))
            url = body.get("next_page_url")  # None on last page
            payload = None                   # only on first request
        time.sleep(AF_API_DELAY_SEC)         # polite pause between calls
        return results

    def get_rent_roll(self) -> list[dict]:
        """
        Returns current leases. Key fields used:
          occupancy_id, property_id, property_name, property_address,
          property_street, property_city, property_state, property_zip,
          unit, tenant, additional_tenants, rent (str), past_due (str),
          lease_from, lease_to, status
        """
        return self._post_report("rent_roll")

    def get_owner_directory(self) -> list[dict]:
        """
        Returns all owners. Key fields used:
          owner_id, name, first_name, last_name, email,
          properties_owned_i_ds  ← comma-separated property_id strings
        """
        return self._post_report("owner_directory")

    def get_tenant_directory(self) -> list[dict]:
        """
        Returns all tenants. Key fields used:
          occupancy_id, phone_numbers (format: "Phone: (xxx) xxx-xxxx"), emails,
          primary_tenant ("Yes"/"No") — filter to primary only for phone lookup.
        """
        return self._post_report("tenant_directory")

    def get_tenant_ledger_month(self, from_date: str, to_date: str) -> list[dict]:
        """
        Returns ALL ledger rows for the date range across all tenants.
        Note: occupancy_id filtering is ignored by AppFolio — always returns all.
        We match payments to units via the payer name field in Python.

        Credit rows (credit != null) = actual payments.
        Debit rows  (debit  != null) = charges (rent, fees, etc.)
        """
        return self._post_report(
            "tenant_ledger",
            {"from_date": from_date, "to_date": to_date}
        )


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def build_owner_property_map(owners: list[dict]) -> dict[int, dict]:
    """
    Returns {property_id: owner_dict} for all owners.
    A property can only have one primary owner; last-write wins if duplicated.
    """
    mapping: dict[int, dict] = {}
    for owner in owners:
        raw = owner.get("properties_owned_i_ds", "") or ""
        for pid_str in raw.split(","):
            pid_str = pid_str.strip()
            if pid_str.isdigit():
                mapping[int(pid_str)] = owner
    return mapping


def normalize_tenant_name(name: str) -> str:
    """
    Normalize AppFolio tenant name to 'First Last' for matching against payer field.
    rent_roll/tenant_directory format: 'Last, First Middle'
    ledger payer format: 'First Last'
    """
    name = (name or "").strip()
    if "," in name:
        parts = name.split(",", 1)
        return f"{parts[1].strip()} {parts[0].strip()}"
    return name


def build_payment_map(ledger_rows: list[dict]) -> dict[str, list[dict]]:
    """
    Returns {normalized_payer_name: [payment_records]} from credit rows only.
    Handles multiple payments per tenant and NSF reversals.

    Payment record: {date, amount, description, is_nsf}
    """
    payments: dict[str, list] = {}
    for row in ledger_rows:
        credit_raw = row.get("credit")
        if not credit_raw:
            continue
        try:
            amount = float(credit_raw)
        except (ValueError, TypeError):
            continue
        if amount == 0:
            continue

        payer = normalize_tenant_name(row.get("payer") or "Unknown")
        desc  = (row.get("description") or "").strip()
        is_nsf = "nsf" in desc.lower() or "reversed" in desc.lower()

        raw_date    = row.get("date", "")
        intended    = detect_intended_month(desc, raw_date)  # (year, month) or None
        payments.setdefault(payer, []).append({
            "date":            raw_date,
            "amount":          amount,
            "description":     _shorten_payment_desc(desc),
            "is_nsf":          is_nsf,
            "intended_month":  intended,  # None = same as calendar month
        })
    return payments


# Month names for intent detection
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3,    "april": 4,
    "may": 5,     "june": 6,     "july": 7,      "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

def detect_intended_month(desc: str, payment_date: str) -> tuple[int, int] | None:
    """
    Parse month name from description to detect if a payment is intended for
    a different month than its calendar date (e.g. "April rent" paid on May 6).

    Returns (intended_year, intended_month) if a mismatch is found, else None.
    Only matches when description contains "[MonthName] rent".
    """
    import re
    desc_lower = desc.lower()
    pattern = r"\b(" + "|".join(_MONTH_NAMES.keys()) + r")\s+rent\b"
    match = re.search(pattern, desc_lower)
    if not match:
        return None  # no month annotation — assume current month

    intended_month_num = _MONTH_NAMES[match.group(1)]
    try:
        pay = date.fromisoformat(payment_date)
    except ValueError:
        return None

    if intended_month_num == pay.month:
        return None  # matches calendar month — no mismatch

    # Infer intended year (e.g. "April rent" paid in May 2026 → April 2026)
    intended_year = pay.year
    if intended_month_num > pay.month + 1:
        # Described month is far ahead — likely prior year (rare edge case)
        intended_year = pay.year - 1
    elif intended_month_num < pay.month - 1:
        # Described month is well behind — could be paying very late
        pass  # same year is correct

    return (intended_year, intended_month_num)


def _shorten_payment_desc(desc: str) -> str:
    """
    Shorten verbose AppFolio payment descriptions to fit calendar event.
    e.g. 'ACH Payment (Reference #CE35-5160)' → 'ACH (#CE35-5160)'
         'Credit Card Payment (Reference #6D28)' → 'Credit Card (#6D28)'
         'Payment (Reference #Zelle) May rent...' → 'Zelle - May rent...'
    """
    import re
    # ACH Payment (Reference #XXXX) → ACH (#XXXX)
    desc = re.sub(r'ACH Payment \(Reference (#[\w-]+)\)', r'ACH ()', desc)
    # Credit Card Payment (Reference #XXXX) → Credit Card (#XXXX)
    desc = re.sub(r'Credit Card Payment \(Reference (#[\w-]+)\)', r'Credit Card ()', desc)
    # Payment (Reference #Zelle) ... → Zelle ...
    desc = re.sub(r'Payment \(Reference #(\w+)\)\s*', r' - ', desc)
    # Trim
    return desc[:80].strip(" -")


def build_tenant_phone_map(tenants: list[dict]) -> dict[int, str]:
    """
    Returns {occupancy_id: phone_number_string} for primary tenants only.
    Strips the "Phone: " prefix AppFolio includes in the field.
    """
    mapping: dict[int, str] = {}
    for t in tenants:
        if t.get("primary_tenant") != "Yes":
            continue
        oid = t.get("occupancy_id")
        raw = (t.get("phone_numbers") or "").strip()
        # Strip label prefix e.g. "Phone: (773) 822-5358" → "(773) 822-5358"
        phone = raw.replace("Phone:", "").replace("Mobile:", "").replace("Fax:", "").strip()
        if oid and phone:
            mapping[int(oid)] = phone
    return mapping


def format_address(row: dict) -> str:
    parts = [
        row.get("property_street", ""),
        row.get("property_city", ""),
        row.get("property_state", ""),
        row.get("property_zip", "") or "",
    ]
    return ", ".join(p for p in parts if p)


def unit_label(row: dict) -> str:
    """Return unit string, or empty for single-family.
    AppFolio already includes 'Unit' in the field value, so don't prepend it.
    """
    u = (row.get("unit") or "").strip()
    return u  # e.g. "Unit 2", "2F", or "" for single-family


def owner_display_name(owner: dict) -> str:
    """Prefer legal name (LLC etc.), fall back to first+last."""
    name = (owner.get("name") or "").strip()
    if name:
        return name
    first = (owner.get("first_name") or "").strip()
    last  = (owner.get("last_name") or "").strip()
    return f"{first} {last}".strip() or "Unknown Owner"


# ---------------------------------------------------------------------------
# Google Calendar manager
# ---------------------------------------------------------------------------
class GoogleCalendarManager:

    def __init__(self):
        sa_info = json.loads(GOOGLE_SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=GOOGLE_SCOPES
        )
        self.service = build("calendar", "v3", credentials=creds)
        self._calendar_cache: dict[str, str] = {}  # owner_name → calendar_id

    # ── Calendar management ──────────────────────────────────────────────────

    def get_or_create_calendar(self, owner_name: str) -> str:
        """Return calendar_id for this owner, creating it if needed."""
        if owner_name in self._calendar_cache:
            return self._calendar_cache[owner_name]

        summary = f"{CALENDAR_PREFIX} · {owner_name} Portfolio"

        # Search existing calendars
        page_token = None
        while True:
            resp = self.service.calendarList().list(pageToken=page_token).execute()
            for cal in resp.get("items", []):
                if cal["summary"] == summary:
                    self._calendar_cache[owner_name] = cal["id"]
                    return cal["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # Create new calendar
        cal = self.service.calendars().insert(body={
            "summary": summary,
            "description": (
                f"Managed by OKPM. Rent tracking for {owner_name}'s portfolio. "
                "Do not edit — auto-synced from AppFolio."
            ),
            "timeZone": "America/Chicago",
        }).execute()
        log.info(f"Created calendar: {summary}")
        # Immediately share with PM account so they can see it
        if PM_EMAIL:
            self._share(cal["id"], PM_EMAIL, role="owner", notify=False)
        self._calendar_cache[owner_name] = cal["id"]
        return cal["id"]

    def _share(self, calendar_id: str, email: str, role: str = "reader", notify: bool = True):
        """Internal: grant a role to an email. Idempotent."""
        if not email:
            return
        try:
            acl = self.service.acl().list(calendarId=calendar_id).execute()
            for rule in acl.get("items", []):
                if rule.get("scope", {}).get("value") == email:
                    return  # already has access
            self.service.acl().insert(
                calendarId=calendar_id,
                body={"scope": {"type": "user", "value": email}, "role": role},
                sendNotifications=notify,
            ).execute()
            log.info(f"Shared calendar ({role}) with {email}")
        except HttpError as e:
            log.warning(f"Could not share calendar with {email}: {e}")

    def share_with_owner(self, calendar_id: str, owner_email: str):
        """Grant read-only access to the property owner. Idempotent."""
        self._share(calendar_id, owner_email, role="reader", notify=True)

    def ensure_pm_access(self, calendar_id: str):
        """Grant the PM account read access to all calendars. Idempotent, no notification."""
        self._share(calendar_id, PM_EMAIL, role="reader", notify=False)

    # ── Event building ───────────────────────────────────────────────────────

    def _build_rent_event(self, unit: dict, status: str, due_date: date) -> dict:
        late_threshold = due_date + timedelta(days=LATE_GRACE_DAYS)
        balance = unit["past_due"]  # total outstanding (live snapshot, spans months)

        # Title: emoji · tenant · unit · property · rent
        # (text label removed — emoji + color already convey status)
        emoji     = emoji_for_status(status)
        unit_part = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = unit['tenant'].split(",")[0].strip()  # primary tenant only
        title = (
            f"{emoji} · {tenant_short} · "
            f"{unit_part}{unit['property_name']} · ${unit['rent']:,.0f}"
        )

        tenants = unit['tenant']
        if unit.get('additional_tenants'):
            tenants += f", {unit['additional_tenants']}"

        # Build payment lines from individual records
        payment_lines = []
        payments = unit.get("payments", [])
        if payments:
            for p in sorted(payments, key=lambda x: x["date"]):
                nsf_tag = " ⚠️ NSF" if p["is_nsf"] else ""
                # Format date as "May 03"
                try:
                    pdate = date.fromisoformat(p["date"]).strftime("%b %d")
                except ValueError:
                    pdate = p["date"]
                payment_lines.append(
                    f"  {pdate}  ${p['amount']:,.2f}  {p['description']}{nsf_tag}"
                )
        else:
            payment_lines.append("  No payments recorded yet")

        # Format balance line — handle credit (negative past_due) clearly
        if balance < 0:
            balance_line = (
                f"Current Balance: $0.00  "
                f"(+ ${abs(balance):,.2f} credit toward next month)"
            )
        else:
            balance_line = f"Current Balance: ${balance:,.2f}"

        desc_lines = [
            f"Tenant(s):       {tenants}",
            (f"{unit['unit_label']}  |  " if unit['unit_label'] else "") +
            f"{unit['address']}",
            f"Phone:           {unit['phone']}",
            "─" * 40,
            f"Monthly Rent:    ${unit['rent']:,.2f}",
            f"Received this month: ${unit['amount_paid']:,.2f}",
            balance_line,
            f"Status:          {status}",
            "─" * 40,
            "Payments received this month:",
        ]
        desc_lines.extend(payment_lines)
        desc_lines += [
            "─" * 40,
            f"Late After:      {late_threshold.strftime('%b %d, %Y')}",
            f"Lease:           {unit['lease_from']} → {unit['lease_to']}",
            "─" * 40,
            "Note: Current Balance is the tenant's total amount owed and may",
            "include unpaid charges carried over from previous months.",
        ]
        description = "\n".join(desc_lines)

        return {
            "summary": title,
            "location": unit["address"],
            "description": description,
            "start": {"date": due_date.isoformat()},
            "end":   {"date": due_date.isoformat()},
            "colorId": color_for_status(status),
            "extendedProperties": {
                "private": {
                    "okpm_occupancy_id": str(unit["occupancy_id"]),
                    "okpm_month":        due_date.strftime("%Y-%m"),
                    "okpm_event_type":   "rent",
                }
            },
        }

    def _build_payment_event(
        self, unit: dict, payment: dict,
        payment_num: int, total_payments: int,
        month_received_total: float,
    ) -> dict:
        """
        One calendar event per individual payment, placed on the payment date.
        This is a pure transaction RECORD — it does not attempt to attribute the
        payment to a specific month's rent (rent is one continuous ledger, so a
        payment may settle a prior month's balance). Status/balance lives on the
        rent-due event, which uses past_due as the source of truth.

        Color: 🟢 green for received, 🔴 red for NSF/reversed.
        """
        pay_date = payment["date"]
        try:
            pay_date_display = date.fromisoformat(pay_date).strftime("%b %d, %Y")
        except ValueError:
            pay_date_display = pay_date

        tenant_display = normalize_tenant_name(unit["tenant"])
        unit_part      = f"{unit['unit_label']} · " if unit["unit_label"] else ""

        if payment["is_nsf"]:
            emoji   = "🔴"
            color   = COLOR_UNPAID
            nsf_tag = " NSF"
        elif payment.get("intended_month"):
            emoji   = "🟡"
            color   = COLOR_PARTIAL   # yellow — received but for a prior month
            nsf_tag = " (late)"
        else:
            emoji   = "✅"
            color   = COLOR_PAID
            nsf_tag = ""

        title = (
            f"{emoji} · {tenant_display} · "
            f"{unit_part}{unit['property_name']} · "
            f"${payment['amount']:,.0f}{nsf_tag}"
        )

        # Month label for context (e.g. "May")
        try:
            month_label = date.fromisoformat(pay_date).strftime("%B")
        except ValueError:
            month_label = "this month"

        nsf_note = "  ⚠️ REVERSED / NSF — funds did not clear" if payment["is_nsf"] else ""

        # Detect if this payment is for a different month than its calendar date
        intended = payment.get("intended_month")  # (year, month) or None
        if intended:
            intended_label = date(intended[0], intended[1], 1).strftime("%B %Y")
            intent_flag = f"⚠️  Applies to {intended_label} charges (paid late)"
        else:
            intent_flag = None

        desc_lines = [
            f"Payment received: {pay_date_display}",
            f"Method:           {payment['description']}{nsf_note}",
            f"Amount:           ${payment['amount']:,.2f}",
        ]
        if intent_flag:
            desc_lines.append(intent_flag)
        desc_lines += [
            f"(Payment {payment_num} of {total_payments} received in {month_label})",
            "─" * 40,
            f"Total received in {month_label}: ${month_received_total:,.2f}",
            f"Monthly rent:     ${unit['rent']:,.2f}",
            "─" * 40,
            f"Tenant:           {tenant_display}",
            (f"{unit['unit_label']}  |  " if unit["unit_label"] else "") + unit["address"],
            f"Phone:            {unit['phone']}",
            "─" * 40,
            "Note: payments apply to the tenant's overall balance and may",
            "settle charges carried from a previous month. See the rent-due",
            "event for current account status.",
        ]

        return {
            "summary":     title,
            "location":    unit["address"],
            "description": "\n".join(desc_lines),
            "start":       {"date": pay_date},
            "end":         {"date": pay_date},
            "colorId":     color,
            "extendedProperties": {
                "private": {
                    "okpm_occupancy_id":  str(unit["occupancy_id"]),
                    "okpm_month":         pay_date[:7],
                    "okpm_event_type":    "payment",
                    "okpm_payment_idx":   str(payment_num - 1),
                }
            },
        }

    def _build_late_event(self, unit: dict, days_late: int) -> dict:
        today_str = date.today().isoformat()
        unit_part    = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = unit['tenant'].split(",")[0].strip()
        title = (
            f"⚠️ · {tenant_short} · {unit_part}"
            f"{unit['property_name']} · ${unit['past_due']:,.0f} owed (Day {days_late})"
        )

        tenants = unit['tenant']
        if unit.get('additional_tenants'):
            tenants += f", {unit['additional_tenants']}"

        description = "\n".join([
            f"Tenant(s):    {tenants}",
            f"Address:      {unit['address']}",
            "─" * 40,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Outstanding:  ${unit['past_due']:,.2f}",
            f"Days Late:    {days_late}",
        ])

        return {
            "summary": title,
            "location": unit["address"],
            "description": description,
            "start": {"date": today_str},
            "end":   {"date": today_str},
            "colorId": COLOR_LATE,
            "extendedProperties": {
                "private": {
                    "okpm_occupancy_id": str(unit["occupancy_id"]),
                    "okpm_month":        today_str[:7],
                    "okpm_event_type":   "late",
                }
            },
        }

    # ── Event upsert / delete ────────────────────────────────────────────────

    def _find_event(
        self, calendar_id: str, occupancy_id: str,
        month: str, event_type: str
    ) -> Optional[str]:
        result = self.service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=[
                f"okpm_occupancy_id={occupancy_id}",
                f"okpm_month={month}",
                f"okpm_event_type={event_type}",
            ],
        ).execute()
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def _find_payment_event(
        self, calendar_id: str, occupancy_id: str,
        month: str, payment_idx: int
    ) -> Optional[str]:
        result = self.service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=[
                f"okpm_occupancy_id={occupancy_id}",
                f"okpm_month={month}",
                f"okpm_event_type=payment",
                f"okpm_payment_idx={payment_idx}",
            ],
        ).execute()
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def upsert_event(self, calendar_id: str, event_body: dict) -> str:
        props = event_body["extendedProperties"]["private"]
        existing_id = self._find_event(
            calendar_id,
            props["okpm_occupancy_id"],
            props["okpm_month"],
            props["okpm_event_type"],
        )
        if existing_id:
            self.service.events().update(
                calendarId=calendar_id,
                eventId=existing_id,
                body=event_body,
            ).execute()
            log.info(
                f"Updated {props['okpm_event_type']} event "
                f"for occupancy {props['okpm_occupancy_id']} ({props['okpm_month']})"
            )
            return existing_id
        else:
            created = self.service.events().insert(
                calendarId=calendar_id, body=event_body
            ).execute()
            log.info(
                f"Created {props['okpm_event_type']} event "
                f"for occupancy {props['okpm_occupancy_id']} ({props['okpm_month']})"
            )
            return created["id"]

    def delete_event(self, calendar_id: str, event_id: str):
        try:
            self.service.events().delete(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            log.info(f"Deleted event {event_id}")
        except HttpError as e:
            if e.resp.status != 410:  # 410 = already gone, safe to ignore
                raise


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------
class StateManager:
    """
    Persists last-known status + event IDs between runs.
    Committed back to the GitHub repo after each sync.

    Key: f"{occupancy_id}_{YYYY-MM}"
    Value: {status, past_due, rent_event_id, late_event_id, last_updated}
    """

    def __init__(self):
        self.path = STATE_FILE
        self.data: dict = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    def _key(self, occupancy_id: str, month: str) -> str:
        return f"{occupancy_id}_{month}"

    def get(self, occupancy_id: str, month: str) -> Optional[dict]:
        return self.data.get(self._key(occupancy_id, month))

    def set(self, occupancy_id: str, month: str, entry: dict):
        entry["last_updated"] = datetime.utcnow().isoformat()
        self.data[self._key(occupancy_id, month)] = entry

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))
        log.info("State saved to state.json")


# ---------------------------------------------------------------------------
# Main sync orchestrator
# ---------------------------------------------------------------------------
class SyncOrchestrator:

    def __init__(self):
        self.af    = AppFolioClient()
        self.gcal  = GoogleCalendarManager()
        self.state = StateManager()

    def run(self):
        log.info("=== OKPM AppFolio → Calendar sync starting ===")
        today      = date.today()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)

        # ── 1. Fetch data (2 API calls total) ───────────────────────────────
        log.info("Fetching rent_roll...")
        rent_roll = self.af.get_rent_roll()
        log.info(f"  {len(rent_roll)} rows")

        log.info("Fetching owner_directory...")
        owners = self.af.get_owner_directory()
        log.info(f"  {len(owners)} owners")

        log.info("Fetching tenant_directory...")
        tenants = self.af.get_tenant_directory()
        log.info(f"  {len(tenants)} tenant rows")

        log.info("Fetching tenant_ledger (current month)...")
        first_of_month = today.replace(day=1).isoformat()
        ledger_rows = self.af.get_tenant_ledger_month(first_of_month, today.isoformat())
        log.info(f"  {len(ledger_rows)} ledger rows")

        # ── 2. Build lookup tables ───────────────────────────────────────────
        prop_to_owner  = build_owner_property_map(owners)
        tenant_phones  = build_tenant_phone_map(tenants)
        payment_map    = build_payment_map(ledger_rows)
        log.info(f"  {len(prop_to_owner)} property→owner mappings")
        log.info(f"  {len(tenant_phones)} tenant phone numbers")
        log.info(f"  {len(payment_map)} tenants with payment records this month")

        # ── 3. Filter to active/current leases only ─────────────────────────
        active = [r for r in rent_roll if r.get("status") == "Current"]
        log.info(f"  {len(active)} current leases to sync")

        # ── 4. Group by owner ────────────────────────────────────────────────
        owner_rows: dict[int, list] = {}  # owner_id → rows
        unmapped: list = []
        for row in active:
            prop_id = row.get("property_id")
            owner   = prop_to_owner.get(prop_id)
            if owner:
                oid = owner["owner_id"]
                owner_rows.setdefault(oid, []).append((row, owner))
            else:
                unmapped.append(row)
                log.warning(
                    f"No owner mapping for property_id={prop_id} "
                    f"({row.get('property_name')}) — skipping"
                )

        # ── 5. Per owner: ensure calendar, share, sync each unit ─────────────
        for owner_id, rows_and_owners in owner_rows.items():
            owner       = rows_and_owners[0][1]
            owner_name  = owner_display_name(owner)
            owner_email = (owner.get("email") or "").strip()

            log.info(
                f"Owner: {owner_name} ({len(rows_and_owners)} units) "
                f"email={owner_email or 'none'}"
            )

            calendar_id = self.gcal.get_or_create_calendar(owner_name)
            self.gcal.ensure_pm_access(calendar_id)   # PM always has owner access
            if owner_email:
                self.gcal.share_with_owner(calendar_id, owner_email)

            for row, _ in rows_and_owners:
                self._sync_unit(row, calendar_id, due_date, today, this_month,
                                tenant_phones, payment_map)

        # ── 6. Persist state ─────────────────────────────────────────────────
        self.state.save()
        log.info("=== Sync complete ===")

    def _month_range(self, from_date: date, to_date: date) -> list[date]:
        """Return list of 1st-of-month dates from from_date's month to to_date's month."""
        months = []
        cur = from_date.replace(day=RENT_DUE_DAY)
        # If due day already passed this month, start from this month still
        end = to_date.replace(day=RENT_DUE_DAY)
        while cur <= end:
            months.append(cur)
            # Advance one month
            month = cur.month + 1
            year  = cur.year + (1 if month > 12 else 0)
            month = month if month <= 12 else 1
            try:
                cur = cur.replace(year=year, month=month, day=RENT_DUE_DAY)
            except ValueError:
                # due day doesn't exist in this month (e.g. 31st in Feb) — use last day
                import calendar as cal_mod
                last = cal_mod.monthrange(year, month)[1]
                cur = cur.replace(year=year, month=month, day=last)
        return months

    def _sync_unit(
        self, row: dict, calendar_id: str,
        due_date: date, today: date, this_month: str,
        tenant_phones: dict,
        payment_map: dict,
    ):
        occupancy_id = str(row["occupancy_id"])
        rent         = float(row.get("rent", 0) or 0)
        past_due     = float(row.get("past_due", 0) or 0)
        status       = classify_status(rent, past_due)

        # Look up individual payment records by matching tenant name to payer
        tenant_normalized = normalize_tenant_name(row.get("tenant", ""))
        payments_this_month = payment_map.get(tenant_normalized, [])
        amount_paid = sum(p["amount"] for p in payments_this_month if not p["is_nsf"])

        # Parse lease end date
        lease_to_str = row.get("lease_to") or ""
        try:
            lease_end = date.fromisoformat(lease_to_str)
        except ValueError:
            # null lease_to = month-to-month — show DEFAULT_LEASE_MONTHS ahead
            m = due_date.month + DEFAULT_LEASE_MONTHS
            y = due_date.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            lease_end = date(y, m, 1)
            log.debug(
                f"  Occupancy {occupancy_id} has no lease end — "
                f"defaulting to {lease_end} ({DEFAULT_LEASE_MONTHS} months ahead)"
            )

        # Normalized unit dict passed to event builders
        unit = {
            "occupancy_id":       occupancy_id,
            "property_name":      row.get("property_name", ""),
            "address":            format_address(row),
            "unit_label":         unit_label(row),
            "tenant":             row.get("tenant", ""),
            "additional_tenants": row.get("additional_tenants", ""),
            "rent":               rent,
            "past_due":           past_due,
            "amount_paid":        amount_paid,
            "payments":           payments_this_month,  # individual payment records
            "phone":              tenant_phones.get(int(occupancy_id), "N/A"),
            "lease_from":         row.get("lease_from", ""),
            "lease_to":           lease_to_str,
        }

        # ── A. Current month: update rent event + payment events ─────────────
        prior         = self.state.get(occupancy_id, this_month)
        n_payments_now = len(unit.get("payments", []))
        n_payments_prior = len(prior.get("payment_event_ids", [])) if prior else 0
        status_changed   = (
            FORCE_REFRESH or
            not (prior and prior["status"] == status and prior["past_due"] == past_due)
        )
        new_payments     = n_payments_now > n_payments_prior

        if status_changed:
            rent_body     = self.gcal._build_rent_event(unit, status, due_date)
            rent_event_id = self.gcal.upsert_event(calendar_id, rent_body)
            late_event_id = self._handle_late_event(
                unit, calendar_id, due_date, today, status,
                existing_late_id=prior.get("late_event_id") if prior else None,
            )
            log.info(f"  Updated current month for occupancy {occupancy_id} → {status}")
        else:
            rent_event_id = prior["rent_event_id"] if prior else None
            late_event_id = self._handle_late_event(
                unit, calendar_id, due_date, today, status,
                existing_late_id=prior.get("late_event_id") if prior else None,
            )
            log.info(f"  No change for occupancy {occupancy_id} current month — skipping rent event")

        # Sync individual payment events (always run — day count and new payments)
        if status_changed or new_payments:
            payment_event_ids = self._sync_payment_events(
                unit, calendar_id, this_month, prior
            )
        else:
            payment_event_ids = prior.get("payment_event_ids", []) if prior else []

        self.state.set(occupancy_id, this_month, {
            "status":            status,
            "past_due":          past_due,
            "rent_event_id":     rent_event_id,
            "late_event_id":     late_event_id,
            "payment_event_ids": payment_event_ids,
        })

        # ── B. Future months: create placeholder events if not yet in state ──
        # Unit dict for future events — always shows as Unpaid, $0 past due
        # Future placeholders show $0 balance — will be updated when that
        # month becomes current. If tenant has a credit now, note it.
        credit_note = abs(unit['past_due']) if unit['past_due'] < 0 else 0.0
        future_unit = {**unit, "past_due": 0.0, "amount_paid": 0.0,
                       "payments": [], "credit_carried": credit_note}
        future_months = self._month_range(
            due_date + timedelta(days=32),  # start from next month
            lease_end
        )
        created = 0
        has_credit = unit["past_due"] < 0

        for i, future_due in enumerate(future_months):
            fmonth       = future_due.strftime("%Y-%m")
            prior_future = self.state.get(occupancy_id, fmonth)

            # The next month (i=0) must be force-updated if tenant has a credit
            # balance — the placeholder may have been created as 🔴 Unpaid but
            # now reflects a projected partial/paid status.
            is_next_month   = (i == 0)
            force_update    = is_next_month and has_credit

            if prior_future and not force_update:
                continue  # frozen placeholder — never overwrite

            if is_next_month and has_credit:
                # Project next month: rent charge posts, credit is applied
                projected_due   = unit["rent"] + unit["past_due"]  # past_due is negative
                projected_paid  = abs(unit["past_due"])             # credit = prepaid amount
                future_status   = classify_status(unit["rent"], projected_due)
                this_unit = {
                    **future_unit,
                    "past_due":   max(0.0, projected_due),
                    "amount_paid": projected_paid,
                }
                log.info(
                    f"  Projecting next month for occupancy {occupancy_id}: "
                    f"credit=${abs(unit['past_due']):,.2f} → "
                    f"projected balance=${max(0, projected_due):,.2f} → {future_status}"
                )
            else:
                future_status = STATUS_UNPAID
                this_unit     = future_unit

            future_body     = self.gcal._build_rent_event(this_unit, future_status, future_due)
            future_event_id = self.gcal.upsert_event(calendar_id, future_body)
            self.state.set(occupancy_id, fmonth, {
                "status":        future_status,
                "past_due":      this_unit["past_due"],
                "rent_event_id": future_event_id,
                "late_event_id": None,
            })
            created += 1

        if created:
            log.info(
                f"  Synced {created} future month events for occupancy {occupancy_id} "
                f"through {lease_end}"
            )

    def _sync_payment_events(
        self, unit: dict, calendar_id: str,
        this_month: str, prior: Optional[dict],
    ) -> list[str]:
        """
        Create or update one calendar event per individual payment this month.
        Events are placed on the actual payment date (not the rent due date).
        Running totals and emoji update as payments accumulate.

        Returns list of Google Calendar event IDs (one per payment).
        Only called for the current month — past months are never touched.
        """
        payments = unit.get("payments", [])
        if not payments:
            return []

        # Sort by date then amount for stable ordering
        sorted_payments = sorted(payments, key=lambda p: (p["date"], p["amount"]))
        total = len(sorted_payments)

        # Total money received this calendar month (excludes NSF reversals)
        month_received = sum(p["amount"] for p in sorted_payments if not p["is_nsf"])

        event_ids = []
        prior_ids = prior.get("payment_event_ids", []) if prior else []

        for i, payment in enumerate(sorted_payments):
            event_body = self.gcal._build_payment_event(
                unit, payment, i + 1, total, month_received
            )

            # Reuse known event ID if we have it, otherwise search
            existing_id = prior_ids[i] if i < len(prior_ids) else None
            if not existing_id:
                existing_id = self.gcal._find_payment_event(
                    calendar_id, str(unit["occupancy_id"]), this_month, i
                )

            if existing_id:
                self.gcal.service.events().update(
                    calendarId=calendar_id,
                    eventId=existing_id,
                    body=event_body,
                ).execute()
                event_ids.append(existing_id)
                log.info(
                    f"  Updated payment event {i+1}/{total} for "
                    f"occupancy {unit['occupancy_id']}"
                )
            else:
                created = self.gcal.service.events().insert(
                    calendarId=calendar_id, body=event_body
                ).execute()
                event_ids.append(created["id"])
                log.info(
                    f"  Created payment event {i+1}/{total} for "
                    f"occupancy {unit['occupancy_id']} on {payment['date']}"
                )

        return event_ids

    def _handle_late_event(
        self, unit: dict, calendar_id: str,
        due_date: date, today: date, status: str,
        existing_late_id: Optional[str],
    ) -> Optional[str]:
        """
        Late event lifecycle:
          - Paid → delete any existing late event
          - Past grace period + unpaid/partial → create/update with current day count
          - Before grace period → no late event yet
        """
        if status == STATUS_PAID:
            if existing_late_id:
                self.gcal.delete_event(calendar_id, existing_late_id)
            return None

        days_late = (today - (due_date + timedelta(days=LATE_GRACE_DAYS))).days
        if days_late > 0:
            late_body = self.gcal._build_late_event(unit, days_late)
            return self.gcal.upsert_event(calendar_id, late_body)

        return existing_late_id  # within grace period, nothing to do


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    SyncOrchestrator().run()