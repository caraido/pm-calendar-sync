"""
OKPM AppFolio → Google Calendar Sync
======================================
Polls AppFolio Plus Reports API (v2) and maintains per-owner Google Calendars.

Model (current month):
  - STATUS EVENT: one per month. Starts on the 1st with outstanding balance.
    First payment migrates it to the payment date, absorbing the transaction.
    Subsequent payments get their own events — status event stays put.
  - ADDITIONAL PAYMENT EVENTS: one per payment after the first, on their dates.
  - LATE EVENT: separate floating event once grace period passes.

Model (future months):
  - Single placeholder on the 1st showing projected balance. Frozen once created
    (except next month when current tenant has a credit balance).

API calls per run: 4 (rent_roll, owner_directory, tenant_directory, tenant_ledger).
Field names verified against live AppFolio API 2026-05-17.
"""

import os, re, json, time, logging, requests
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APPFOLIO_DB_NAME       = os.environ["APPFOLIO_DB_NAME"]
APPFOLIO_CLIENT_ID     = os.environ["APPFOLIO_CLIENT_ID"]
APPFOLIO_CLIENT_SECRET = os.environ["APPFOLIO_CLIENT_SECRET"]
GOOGLE_SA_JSON         = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GOOGLE_SCOPES          = ["https://www.googleapis.com/auth/calendar"]

LATE_GRACE_DAYS      = int(os.environ.get("LATE_GRACE_DAYS", 5))
RENT_DUE_DAY         = int(os.environ.get("RENT_DUE_DAY", 1))
PM_EMAIL             = os.environ.get("PM_EMAIL", "")
DEFAULT_LEASE_MONTHS = int(os.environ.get("DEFAULT_LEASE_MONTHS", 12))
FORCE_REFRESH        = os.environ.get("FORCE_REFRESH", "").lower() == "true"
STATE_FILE           = Path("state.json")
CALENDAR_PREFIX      = "OKPM"
AF_API_DELAY_SEC     = 2.0

_AF_BASE    = f"https://{APPFOLIO_CLIENT_ID}:{APPFOLIO_CLIENT_SECRET}@{APPFOLIO_DB_NAME}.appfolio.com/api/v2/reports"
_AF_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Google Calendar color IDs
COLOR_PAID    = "2"   # sage green
COLOR_PREPAID = "4"   # flamingo pink
COLOR_PARTIAL = "5"   # banana yellow
COLOR_UNPAID  = "11"  # tomato red
COLOR_LATE    = "11"  # tomato red

# ---------------------------------------------------------------------------
# Status system
# ---------------------------------------------------------------------------
STATUS_PAID    = "✅ Paid"
STATUS_PREPAID = "🩷 Prepaid"
STATUS_PARTIAL = "🟡 Partial"
STATUS_UNPAID  = "🔴 Unpaid"
STATUS_LATE    = "🔴 Late"


def classify_status(rent: float, past_due: float) -> str:
    if past_due < 0:   return STATUS_PREPAID
    elif past_due == 0: return STATUS_PAID
    elif past_due < rent: return STATUS_PARTIAL
    else:               return STATUS_UNPAID


def color_for_status(status: str) -> str:
    return {
        STATUS_PAID:    COLOR_PAID,
        STATUS_PREPAID: COLOR_PREPAID,
        STATUS_PARTIAL: COLOR_PARTIAL,
        STATUS_UNPAID:  COLOR_UNPAID,
        STATUS_LATE:    COLOR_LATE,
    }.get(status, COLOR_UNPAID)


def emoji_for_status(status: str) -> str:
    return {
        STATUS_PAID:    "✅",
        STATUS_PREPAID: "🩷",
        STATUS_PARTIAL: "🟡",
        STATUS_UNPAID:  "🔴",
        STATUS_LATE:    "🔴",
    }.get(status, "🔴")


# ---------------------------------------------------------------------------
# AppFolio client
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

    def get_rent_roll(self)          -> list[dict]: return self._post_report("rent_roll")
    def get_owner_directory(self)    -> list[dict]: return self._post_report("owner_directory")
    def get_tenant_directory(self)   -> list[dict]: return self._post_report("tenant_directory")
    def get_tenant_ledger_month(self, from_date: str, to_date: str) -> list[dict]:
        return self._post_report("tenant_ledger", {"from_date": from_date, "to_date": to_date})


# ---------------------------------------------------------------------------
# Data helpers
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
    """Return (year, month) if description names a different month than payment date."""
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
    m = {}
    for o in owners:
        for pid in (o.get("properties_owned_i_ds") or "").split(","):
            if pid.strip().isdigit():
                m[int(pid.strip())] = o
    return m


def build_tenant_info_map(tenants: list[dict]) -> dict:
    """
    Returns {occupancy_id: {phone, late_fee_desc, grace_days}} for primary tenants.
    late_fee_desc: human-readable policy, e.g. 'Flat $30.00 after 5 days'
    """
    m = {}
    for t in tenants:
        if t.get("primary_tenant") != "Yes": continue
        oid = t.get("occupancy_id")
        if not oid: continue
        raw_phone = (t.get("phone_numbers") or "").strip()
        phone = raw_phone.replace("Phone:","").replace("Mobile:","").replace("Fax:","").strip()
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
    """Returns {normalized_payer_name: [payment_records]} from credit rows only."""
    payments = {}
    for row in ledger_rows:
        try: amount = float(row.get("credit") or 0)
        except: continue
        if amount <= 0: continue  # skip zero and negative (NSF clawback entries)
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
    """
    Reconstruct account balance after each payment, backward from current past_due.
    NSF payments excluded (funds never cleared).
    balance_after[i] = current_past_due + sum(non-NSF amounts after i)
    """
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
    return (row.get("unit") or "").strip()


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
            json.loads(GOOGLE_SA_JSON), scopes=GOOGLE_SCOPES)
        self.service = build("calendar", "v3", credentials=creds)
        self._cal_cache: dict = {}

    # ── Calendar management ──────────────────────────────────────────────────

    def get_or_create_calendar(self, owner_name: str) -> str:
        if owner_name in self._cal_cache: return self._cal_cache[owner_name]
        summary = f"{CALENDAR_PREFIX} · {owner_name} Portfolio"
        page_token = None
        while True:
            resp = self.service.calendarList().list(pageToken=page_token).execute()
            for cal in resp.get("items", []):
                if cal["summary"] == summary:
                    self._cal_cache[owner_name] = cal["id"]; return cal["id"]
            page_token = resp.get("nextPageToken")
            if not page_token: break
        cal = self.service.calendars().insert(body={
            "summary": summary,
            "description": f"Managed by OKPM. Rent tracking for {owner_name}'s portfolio. Do not edit — auto-synced from AppFolio.",
            "timeZone": "America/Chicago",
        }).execute()
        log.info(f"Created calendar: {summary}")
        if PM_EMAIL: self._share(cal["id"], PM_EMAIL, role="reader", notify=False)
        self._cal_cache[owner_name] = cal["id"]
        return cal["id"]

    def _share(self, calendar_id: str, email: str, role: str = "reader", notify: bool = True):
        if not email: return
        try:
            acl = self.service.acl().list(calendarId=calendar_id).execute()
            for rule in acl.get("items", []):
                if rule.get("scope", {}).get("value") == email: return
            self.service.acl().insert(calendarId=calendar_id,
                body={"scope": {"type": "user", "value": email}, "role": role},
                sendNotifications=notify).execute()
            log.info(f"Shared calendar ({role}) with {email}")
        except HttpError as e:
            log.warning(f"Could not share with {email}: {e}")

    def share_with_owner(self, calendar_id: str, email: str):
        self._share(calendar_id, email, role="reader", notify=True)

    def ensure_pm_access(self, calendar_id: str):
        self._share(calendar_id, PM_EMAIL, role="reader", notify=False)

    # ── Event builders ───────────────────────────────────────────────────────

    def _build_status_event(
        self, unit: dict, event_status: str, event_date: date,
        first_payment: Optional[dict] = None,
        balance_after_first: Optional[float] = None,
        total_payments: int = 0,
    ) -> dict:
        """
        The primary per-month event.
        No payment  → on the 1st, shows outstanding balance.
        First payment → on payment date, absorbs transaction + shows running status.
        """
        emoji        = emoji_for_status(event_status)
        unit_part    = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = unit['tenant'].split(",")[0].strip()
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
        late_after = (due_date_this_month + timedelta(days=unit.get('grace_days', LATE_GRACE_DAYS))).strftime('%b %d, %Y')

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
            nsf_note = "  ⚠️ REVERSED / NSF" if first_payment['is_nsf'] else ""
            intended  = first_payment.get('intended_month')
            bal       = balance_after_first if balance_after_first is not None else unit['past_due']
            remaining = max(0.0, bal)
            has_credit_now = bal < 0

            desc += [
                "─" * 40,
                f"Payment {1} of {total_payments}",
                f"Date:         {pay_display}",
                f"Method:       {first_payment['description']}{nsf_note}",
                f"Amount:       ${first_payment['amount']:,.2f}",
            ]
            if intended:
                desc.append(f"              ⚠️ Applies to {date(intended[0],intended[1],1).strftime('%B %Y')} charges")
            credit_suffix = f"  (+ ${abs(bal):,.2f} credit toward next month)" if has_credit_now else ""
            desc += [
                "─" * 40,
                f"Received in {month_label}: ${unit['amount_paid']:,.2f}",
                f"Monthly Rent: ${unit['rent']:,.2f}",
                f"Balance:      ${remaining:,.2f}{credit_suffix}",
                f"Status:       {event_status}",
            ]
        else:
            # No payment yet
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
        """
        Event for each payment after the first. Emoji reflects cumulative balance
        after this payment (reconstructed from past_due).
        """
        pay_date    = payment["date"]
        pay_status  = classify_status(unit['rent'], running_balance)
        pay_emoji   = emoji_for_status(pay_status)
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

        nsf_note = "  ⚠️ REVERSED / NSF" if payment['is_nsf'] else ""
        intended  = payment.get('intended_month')
        bal_display = max(0.0, running_balance)
        has_credit  = running_balance < 0

        desc = [
            f"Payment {payment_num} of {total_payments} in {month_label}",
            f"Date:         {pay_display}",
            f"Method:       {payment['description']}{nsf_note}",
            f"Amount:       ${payment['amount']:,.2f}",
        ]
        if intended:
            desc.append(f"              ⚠️ Applies to {date(intended[0],intended[1],1).strftime('%B %Y')} charges")
        desc += [
            "─" * 40,
            f"Received in {month_label}: ${month_received:,.2f}",
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Balance after this payment: ${bal_display:,.2f}" + (f"  (+ ${abs(running_balance):,.2f} credit)" if has_credit else ""),
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
        """Simple future-month placeholder on the 1st. Frozen once created."""
        emoji     = emoji_for_status(status)
        unit_part = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = unit['tenant'].split(",")[0].strip()
        outstanding = max(0.0, unit['past_due'])
        title = (
            f"{emoji} · {tenant_short} · "
            f"{unit_part}{unit['property_name']} · "
            f"${outstanding:,.0f} due"
        )
        late_after = (due_date + timedelta(days=unit.get('grace_days', LATE_GRACE_DAYS))).strftime('%b %d, %Y')
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
                "okpm_event_type":   "rent",   # keep 'rent' for frozen placeholders
            }},
        }

    def _build_late_event(self, unit: dict, days_late: int) -> dict:
        unit_part    = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        tenant_short = unit['tenant'].split(",")[0].strip()
        today_str    = date.today().isoformat()
        title = (
            f"🔴 · {tenant_short} · {unit_part}"
            f"{unit['property_name']} · ${unit['past_due']:,.0f} owed (Day {days_late})"
        )
        tenants = unit['tenant']
        if unit.get('additional_tenants'): tenants += f", {unit['additional_tenants']}"
        desc = [
            f"Tenant(s):    {tenants}",
            f"Address:      {unit['address']}",
            "─" * 40,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Outstanding:  ${unit['past_due']:,.2f}",
            f"Days Late:    {days_late}",
            f"Late Fee:     {unit.get('late_fee_desc','N/A')}",
        ]
        return {
            "summary": title, "location": unit['address'],
            "description": "\n".join(desc),
            "start": {"date": today_str}, "end": {"date": today_str},
            "colorId": COLOR_LATE,
            "extendedProperties": {"private": {
                "okpm_occupancy_id": str(unit['occupancy_id']),
                "okpm_month":        today_str[:7],
                "okpm_event_type":   "late",
            }},
        }

    # ── Event find / upsert / delete ─────────────────────────────────────────

    def _find_event(self, calendar_id: str, occupancy_id: str, month: str, event_type: str) -> Optional[str]:
        result = self.service.events().list(calendarId=calendar_id, privateExtendedProperty=[
            f"okpm_occupancy_id={occupancy_id}", f"okpm_month={month}", f"okpm_event_type={event_type}",
        ]).execute()
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def _find_status_event(self, calendar_id: str, occupancy_id: str, month: str) -> Optional[str]:
        """Search for status event; falls back to old 'rent' type for backward compat."""
        return (self._find_event(calendar_id, occupancy_id, month, "status") or
                self._find_event(calendar_id, occupancy_id, month, "rent"))

    def _find_payment_event(self, calendar_id: str, occupancy_id: str, month: str, idx: int) -> Optional[str]:
        result = self.service.events().list(calendarId=calendar_id, privateExtendedProperty=[
            f"okpm_occupancy_id={occupancy_id}", f"okpm_month={month}",
            f"okpm_event_type=payment", f"okpm_payment_idx={idx}",
        ]).execute()
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def _update_or_create(self, calendar_id: str, event_id: Optional[str], body: dict) -> str:
        if event_id:
            self.service.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
            return event_id
        created = self.service.events().insert(calendarId=calendar_id, body=body).execute()
        return created["id"]

    def upsert_event(self, calendar_id: str, event_body: dict) -> str:
        props = event_body["extendedProperties"]["private"]
        existing = self._find_event(calendar_id, props["okpm_occupancy_id"],
                                    props["okpm_month"], props["okpm_event_type"])
        return self._update_or_create(calendar_id, existing, event_body)

    def delete_event(self, calendar_id: str, event_id: str):
        try:
            self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            log.info(f"Deleted event {event_id}")
        except HttpError as e:
            if e.resp.status != 410: raise


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------
class StateManager:
    """
    Per occupancy+month:
    {
      status, past_due,
      status_event_id,    ← the moving status/first-payment event
      status_event_date,  ← current date of that event (to detect migration)
      late_event_id,
      payment_event_ids,  ← additional payments only (idx 1+)
      last_updated
    }
    """
    def __init__(self):
        self.path = STATE_FILE
        self.data: dict = {}
        if self.path.exists(): self.data = json.loads(self.path.read_text())

    def _key(self, oid: str, month: str) -> str: return f"{oid}_{month}"
    def get(self, oid: str, month: str) -> Optional[dict]: return self.data.get(self._key(oid, month))
    def set(self, oid: str, month: str, entry: dict):
        entry["last_updated"] = datetime.utcnow().isoformat()
        self.data[self._key(oid, month)] = entry
    def save(self): self.path.write_text(json.dumps(self.data, indent=2))


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------
class SyncOrchestrator:

    def __init__(self):
        self.af    = AppFolioClient()
        self.gcal  = GoogleCalendarManager()
        self.state = StateManager()

    def run(self):
        log.info("=== OKPM sync starting ===")
        today      = date.today()
        this_month = today.strftime("%Y-%m")
        due_date   = date(today.year, today.month, RENT_DUE_DAY)

        log.info("Fetching rent_roll...")
        rent_roll = self.af.get_rent_roll()
        log.info(f"Fetching owner_directory...")
        owners = self.af.get_owner_directory()
        log.info(f"Fetching tenant_directory...")
        tenants = self.af.get_tenant_directory()
        log.info(f"Fetching tenant_ledger (current month)...")
        ledger = self.af.get_tenant_ledger_month(today.replace(day=1).isoformat(), today.isoformat())

        prop_to_owner = build_owner_property_map(owners)
        tenant_info   = build_tenant_info_map(tenants)
        payment_map   = build_payment_map(ledger)
        active        = [r for r in rent_roll if r.get("status") == "Current"]
        log.info(f"  {len(active)} active leases, {len(payment_map)} with payments this month")

        owner_rows: dict = {}
        for row in active:
            owner = prop_to_owner.get(row.get("property_id"))
            if owner:
                owner_rows.setdefault(owner["owner_id"], []).append((row, owner))
            else:
                log.warning(f"No owner for property_id={row.get('property_id')} — skipping")

        for owner_id, rows_and_owners in owner_rows.items():
            owner      = rows_and_owners[0][1]
            owner_name = owner_display_name(owner)
            owner_email = (owner.get("email") or "").strip()
            log.info(f"Owner: {owner_name} ({len(rows_and_owners)} units)")
            calendar_id = self.gcal.get_or_create_calendar(owner_name)
            self.gcal.ensure_pm_access(calendar_id)
            if owner_email: self.gcal.share_with_owner(calendar_id, owner_email)
            for row, _ in rows_and_owners:
                self._sync_unit(row, calendar_id, due_date, today, this_month, tenant_info, payment_map)

        self.state.save()
        log.info("=== Sync complete ===")

    def _month_range(self, from_date: date, to_date: date) -> list[date]:
        months, cur = [], from_date.replace(day=RENT_DUE_DAY)
        end = to_date.replace(day=RENT_DUE_DAY)
        while cur <= end:
            months.append(cur)
            m = cur.month + 1; y = cur.year + (1 if m > 12 else 0); m = m if m <= 12 else 1
            try: cur = cur.replace(year=y, month=m, day=RENT_DUE_DAY)
            except ValueError:
                import calendar as cm
                cur = cur.replace(year=y, month=m, day=cm.monthrange(y, m)[1])
        return months

    def _make_unit(self, row: dict, tenant_info: dict, payment_map: dict) -> dict:
        oid         = str(row["occupancy_id"])
        rent        = float(row.get("rent", 0) or 0)
        past_due    = float(row.get("past_due", 0) or 0)
        info        = tenant_info.get(int(oid), {})
        t_norm      = normalize_tenant_name(row.get("tenant", ""))
        payments    = payment_map.get(t_norm, [])
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

    def _sync_unit(self, row: dict, calendar_id: str, due_date: date,
                   today: date, this_month: str, tenant_info: dict, payment_map: dict):
        unit    = self._make_unit(row, tenant_info, payment_map)
        oid     = unit["occupancy_id"]
        rent    = unit["rent"]
        past_due= unit["past_due"]
        status  = classify_status(rent, past_due)

        # Lease end
        try: lease_end = date.fromisoformat(unit["lease_to"])
        except ValueError:
            m = due_date.month + DEFAULT_LEASE_MONTHS
            y = due_date.year + (m-1)//12; m = ((m-1)%12)+1
            lease_end = date(y, m, 1)

        # ── A. Current month ─────────────────────────────────────────────────
        sorted_payments = sorted(unit["payments"], key=lambda p: (p["date"], -p["amount"]))
        balances        = compute_running_balances(sorted_payments, past_due)
        prior           = self.state.get(oid, this_month)

        # Status event: sits on 1st if no payments, migrates to first payment date
        if sorted_payments:
            status_event_date = date.fromisoformat(sorted_payments[0]["date"])
            first_pay         = sorted_payments[0]
            event_status      = classify_status(rent, balances[0])
        else:
            status_event_date = due_date
            first_pay         = None
            event_status      = status

        prior_status_id   = (prior.get("status_event_id") or
                             prior.get("rent_event_id")) if prior else None
        prior_status_date = prior.get("status_event_date", due_date.isoformat()) if prior else due_date.isoformat()
        date_changed      = prior_status_date != status_event_date.isoformat()
        data_changed      = not (prior and prior["status"] == status and prior["past_due"] == past_due)
        new_payments      = len(sorted_payments) > (len(prior.get("payment_event_ids", [])) + (1 if prior_status_id and prior_status_date != due_date.isoformat() else 0)) if prior else bool(sorted_payments)

        if FORCE_REFRESH or date_changed or data_changed:
            body = self.gcal._build_status_event(
                unit, event_status, status_event_date,
                first_pay,
                balances[0] if balances else None,
                total_payments=len(sorted_payments),
            )
            # Find existing event (either via state ID or search)
            existing_id = prior_status_id or self.gcal._find_status_event(calendar_id, oid, this_month)
            status_event_id = self.gcal._update_or_create(calendar_id, existing_id, body)
            log.info(f"  Status event for {oid}: {event_status} on {status_event_date}")
        else:
            status_event_id = prior_status_id
            log.info(f"  No change for {oid} — skipping status event")

        # Additional payment events (idx 1+)
        if FORCE_REFRESH or data_changed or new_payments:
            payment_event_ids = self._sync_additional_payments(
                unit, calendar_id, this_month, sorted_payments[1:], balances[1:], prior)
        else:
            payment_event_ids = prior.get("payment_event_ids", []) if prior else []

        # Late event
        late_event_id = self._handle_late_event(
            unit, calendar_id, due_date, today, status,
            prior.get("late_event_id") if prior else None)

        self.state.set(oid, this_month, {
            "status":            status,
            "past_due":          past_due,
            "status_event_id":   status_event_id,
            "status_event_date": status_event_date.isoformat(),
            "late_event_id":     late_event_id,
            "payment_event_ids": payment_event_ids,
        })

        # ── B. Future months ─────────────────────────────────────────────────
        future_unit = {**unit, "past_due": 0.0, "amount_paid": 0.0, "payments": []}
        has_credit  = past_due < 0

        for i, fdue in enumerate(self._month_range(due_date + timedelta(days=32), lease_end)):
            fmonth = fdue.strftime("%Y-%m")
            prior_f = self.state.get(oid, fmonth)
            is_next = (i == 0)

            if prior_f and not (is_next and has_credit): continue

            if is_next and has_credit:
                projected = rent + past_due  # past_due negative
                fut_status = classify_status(rent, projected)
                this_fu = {**future_unit, "past_due": max(0.0, projected), "amount_paid": abs(past_due)}
                log.info(f"  Next month {oid}: credit=${abs(past_due):,.2f} → balance=${max(0,projected):,.2f}")
            else:
                fut_status = STATUS_UNPAID
                this_fu = future_unit

            body = self.gcal._build_future_placeholder(this_fu, fut_status, fdue)
            eid  = self.gcal.upsert_event(calendar_id, body)
            self.state.set(oid, fmonth, {
                "status": fut_status, "past_due": this_fu["past_due"],
                "rent_event_id": eid, "late_event_id": None,
            })

    def _sync_additional_payments(
        self, unit: dict, calendar_id: str, this_month: str,
        additional: list[dict], balances: list[float], prior: Optional[dict],
    ) -> list[str]:
        """Sync payment events for idx 1+ (first payment is absorbed into status event)."""
        prior_ids    = prior.get("payment_event_ids", []) if prior else []
        month_recv   = unit["amount_paid"]
        total        = len(additional) + 1  # +1 because status event absorbs payment 0
        event_ids    = []

        for i, (payment, balance) in enumerate(zip(additional, balances)):
            body = self.gcal._build_additional_payment_event(
                unit, payment, i + 2, total, balance, month_recv)  # payment_num starts at 2
            existing = prior_ids[i] if i < len(prior_ids) else None
            if not existing:
                existing = self.gcal._find_payment_event(calendar_id, unit["occupancy_id"], this_month, i + 1)
            eid = self.gcal._update_or_create(calendar_id, existing, body)
            event_ids.append(eid)
            log.info(f"  Payment {i+2}/{total} for {unit['occupancy_id']} on {payment['date']}")

        return event_ids

    def _handle_late_event(self, unit: dict, calendar_id: str,
                           due_date: date, today: date, status: str,
                           existing_late_id: Optional[str]) -> Optional[str]:
        if status in (STATUS_PAID, STATUS_PREPAID):
            if existing_late_id: self.gcal.delete_event(calendar_id, existing_late_id)
            return None
        days_late = (today - (due_date + timedelta(days=LATE_GRACE_DAYS))).days
        if days_late > 0:
            return self.gcal.upsert_event(calendar_id, self.gcal._build_late_event(unit, days_late))
        return existing_late_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    SyncOrchestrator().run()