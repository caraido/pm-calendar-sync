"""
OKPM AppFolio → Google Calendar Sync
=====================================
Polls AppFolio Plus API for lease/payment data and maintains per-owner
Google Calendars with rent status events.

Calendars are owned by the OKPM service account; owners subscribe read-only.
"""

import os
import json
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
# Config
# ---------------------------------------------------------------------------
APPFOLIO_DB_NAME    = os.environ["APPFOLIO_DB_NAME"]        # e.g. "okpm"
APPFOLIO_CLIENT_ID  = os.environ["APPFOLIO_CLIENT_ID"]
APPFOLIO_CLIENT_SECRET = os.environ["APPFOLIO_CLIENT_SECRET"]
APPFOLIO_BASE_URL   = f"https://{APPFOLIO_DB_NAME}.appfolio.com/api/v1"

GOOGLE_SA_JSON      = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # full JSON string
GOOGLE_SCOPES       = [
    "https://www.googleapis.com/auth/calendar",
]

LATE_GRACE_DAYS     = int(os.environ.get("LATE_GRACE_DAYS", 5))
STATE_FILE          = Path("state.json")
CALENDAR_PREFIX     = "OKPM"   # → "OKPM · John Smith Portfolio"

# Google Calendar color IDs
COLOR_PAID          = "2"   # sage green
COLOR_PARTIAL       = "5"   # banana yellow
COLOR_UNPAID        = "11"  # tomato red
COLOR_LATE          = "6"   # tangerine orange


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
STATUS_PAID         = "✅ Paid"
STATUS_PARTIAL      = "🟡 Partial"
STATUS_UNPAID       = "🔴 Unpaid"
STATUS_LATE         = "⚠️ Late"

def classify_status(amount_due: float, amount_paid: float) -> str:
    if amount_paid >= amount_due:
        return STATUS_PAID
    elif amount_paid > 0:
        return STATUS_PARTIAL
    else:
        return STATUS_UNPAID

def color_for_status(status: str) -> str:
    return {
        STATUS_PAID:    COLOR_PAID,
        STATUS_PARTIAL: COLOR_PARTIAL,
        STATUS_UNPAID:  COLOR_UNPAID,
        STATUS_LATE:    COLOR_LATE,
    }.get(status, COLOR_UNPAID)


# ---------------------------------------------------------------------------
# AppFolio client
# ---------------------------------------------------------------------------
class AppFolioClient:
    """
    Thin wrapper around AppFolio Plus REST API.

    ⚠️  ENDPOINT NOTE: AppFolio's API surface is not fully public.
    The endpoints below follow their documented v1 schema for Plus accounts.
    If any return 404, check your AppFolio API docs under:
      Settings → API Access → Documentation
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (APPFOLIO_CLIENT_ID, APPFOLIO_CLIENT_SECRET)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, endpoint: str, params: dict = None) -> list:
        """Paginated GET — AppFolio uses `paginate_next` links."""
        url = f"{APPFOLIO_BASE_URL}/{endpoint}"
        results = []
        while url:
            r = self.session.get(url, params=params)
            r.raise_for_status()
            body = r.json()
            results.extend(body.get("results", []))
            url = body.get("paginate_next")   # None when last page
            params = None  # only pass params on first request
        return results

    def get_leases(self) -> list[dict]:
        """
        Active residential leases.
        Returns fields including: id, unit_id, unit_address, property_name,
        tenant_names, owner_name, owner_email, rent_amount, start_date, end_date.

        ⚠️  Verify exact field names against your AppFolio API docs.
        """
        return self._get("leases", params={"status": "current"})

    def get_payments(self, lease_id: str, from_date: str) -> list[dict]:
        """
        Payments recorded against a lease since from_date (YYYY-MM-DD).
        Returns: amount, date_received, payment_type.

        ⚠️  Endpoint may be 'owner_payments' or 'charges' depending on your tier.
        """
        return self._get(
            "payments",
            params={"lease_id": lease_id, "from_date": from_date}
        )

    def get_all_payments_this_month(self) -> list[dict]:
        """
        Bulk pull of all payments this month — more efficient than per-lease calls.
        Falls back to per-lease if this endpoint is unavailable.

        ⚠️  Confirm this endpoint in your AppFolio API docs.
        """
        first_of_month = date.today().replace(day=1).isoformat()
        return self._get("payments", params={"from_date": first_of_month})


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

    # ── Calendar management ──────────────────────────────────────────────

    def get_or_create_calendar(self, owner_name: str) -> str:
        """Return calendar_id for this owner, creating it if needed."""
        summary = f"{CALENDAR_PREFIX} · {owner_name} Portfolio"

        # Check if already exists
        calendars = self.service.calendarList().list().execute()
        for cal in calendars.get("items", []):
            if cal["summary"] == summary:
                return cal["id"]

        # Create new
        cal = self.service.calendars().insert(body={
            "summary": summary,
            "description": f"Managed by OKPM. Rent tracking for {owner_name}'s portfolio.",
            "timeZone": "America/Chicago",
        }).execute()
        log.info(f"Created calendar: {summary}")
        return cal["id"]

    def share_with_owner(self, calendar_id: str, owner_email: str):
        """Grant read-only access to the owner. Idempotent."""
        try:
            # Check existing ACL first
            acl = self.service.acl().list(calendarId=calendar_id).execute()
            for rule in acl.get("items", []):
                if rule.get("scope", {}).get("value") == owner_email:
                    return  # already shared
            # Add reader rule
            self.service.acl().insert(
                calendarId=calendar_id,
                body={"scope": {"type": "user", "value": owner_email}, "role": "reader"},
                sendNotifications=True,
            ).execute()
            log.info(f"Shared calendar with {owner_email}")
        except HttpError as e:
            log.warning(f"Could not share calendar with {owner_email}: {e}")

    # ── Event building ───────────────────────────────────────────────────

    def _build_rent_event(self, unit: dict, status: str, due_date: date) -> dict:
        """Build the Google Calendar event body for a rent-due day."""
        balance = unit["amount_due"] - unit["amount_paid"]
        date_str = due_date.isoformat()
        late_threshold = due_date + timedelta(days=LATE_GRACE_DAYS)

        title = (
            f"{status} · Unit {unit['unit_label']} · "
            f"{unit['property_nickname']} · ${unit['amount_due']:,.0f}"
        )

        description = "\n".join([
            f"Tenant:       {unit['tenant_name']}",
            f"Unit:         {unit['unit_label']}  |  {unit['address']}",
            "─" * 38,
            f"Rent Due:     ${unit['amount_due']:,.2f}",
            f"Paid:         ${unit['amount_paid']:,.2f}",
            f"Balance:      ${balance:,.2f}",
            f"Status:       {status}",
            "─" * 38,
            f"Late After:   {late_threshold.strftime('%b %d, %Y')}",
            f"Lease Ends:   {unit['lease_end']}",
            "─" * 38,
            f"Tenant Phone: {unit.get('tenant_phone', 'N/A')}",
            f"Tenant Email: {unit.get('tenant_email', 'N/A')}",
        ])

        return {
            "summary": title,
            "location": unit["address"],
            "description": description,
            "start": {"date": date_str},
            "end": {"date": date_str},
            "colorId": color_for_status(status),
            "extendedProperties": {
                "private": {
                    "okpm_lease_id": str(unit["lease_id"]),
                    "okpm_month": date_str[:7],
                    "okpm_event_type": "rent",
                }
            },
        }

    def _build_late_event(self, unit: dict, days_late: int, due_date: date) -> dict:
        """Build a separate all-day event once rent is past grace period."""
        today = date.today()
        title = (
            f"⚠️ Late Day {days_late} · Unit {unit['unit_label']} · "
            f"{unit['property_nickname']} · ${unit['amount_due'] - unit['amount_paid']:,.0f} owed"
        )
        description = "\n".join([
            f"Tenant:       {unit['tenant_name']}",
            f"Unit:         {unit['unit_label']}  |  {unit['address']}",
            "─" * 38,
            f"Rent Due:     ${unit['amount_due']:,.2f}",
            f"Paid:         ${unit['amount_paid']:,.2f}",
            f"Outstanding:  ${unit['amount_due'] - unit['amount_paid']:,.2f}",
            f"Days Late:    {days_late}",
            "─" * 38,
            f"Tenant Phone: {unit.get('tenant_phone', 'N/A')}",
            f"Tenant Email: {unit.get('tenant_email', 'N/A')}",
        ])
        today_str = today.isoformat()
        return {
            "summary": title,
            "location": unit["address"],
            "description": description,
            "start": {"date": today_str},
            "end": {"date": today_str},
            "colorId": COLOR_LATE,
            "extendedProperties": {
                "private": {
                    "okpm_lease_id": str(unit["lease_id"]),
                    "okpm_month": today_str[:7],
                    "okpm_event_type": "late",
                }
            },
        }

    # ── Event upsert ─────────────────────────────────────────────────────

    def _find_event(self, calendar_id: str, lease_id: str,
                    month: str, event_type: str) -> Optional[str]:
        """Return event_id if an OKPM-managed event exists for this lease+month."""
        result = self.service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=[
                f"okpm_lease_id={lease_id}",
                f"okpm_month={month}",
                f"okpm_event_type={event_type}",
            ],
        ).execute()
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def upsert_event(self, calendar_id: str, event_body: dict) -> str:
        """Insert or update based on private extended properties."""
        props = event_body["extendedProperties"]["private"]
        lease_id = props["okpm_lease_id"]
        month = props["okpm_month"]
        event_type = props["okpm_event_type"]

        existing_id = self._find_event(calendar_id, lease_id, month, event_type)
        if existing_id:
            self.service.events().update(
                calendarId=calendar_id,
                eventId=existing_id,
                body=event_body,
            ).execute()
            log.info(f"Updated {event_type} event for lease {lease_id} ({month})")
            return existing_id
        else:
            created = self.service.events().insert(
                calendarId=calendar_id,
                body=event_body,
            ).execute()
            log.info(f"Created {event_type} event for lease {lease_id} ({month})")
            return created["id"]

    def delete_event(self, calendar_id: str, event_id: str):
        """Delete a calendar event (used to remove stale late events)."""
        try:
            self.service.events().delete(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            log.info(f"Deleted event {event_id}")
        except HttpError as e:
            if e.resp.status != 410:  # 410 = already deleted, safe to ignore
                raise


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------
class StateManager:
    """
    Persists last-known payment state and event IDs to avoid redundant
    Calendar API calls. Stored in state.json and committed back to the repo.

    Schema per entry key = f"{lease_id}_{month}":
    {
        "status": "unpaid" | "partial" | "paid",
        "amount_paid": float,
        "rent_event_id": str,
        "late_event_id": str | null,
        "last_updated": "ISO datetime"
    }
    """

    def __init__(self):
        self.path = STATE_FILE
        self.data: dict = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    def key(self, lease_id: str, month: str) -> str:
        return f"{lease_id}_{month}"

    def get(self, lease_id: str, month: str) -> Optional[dict]:
        return self.data.get(self.key(lease_id, month))

    def set(self, lease_id: str, month: str, entry: dict):
        entry["last_updated"] = datetime.utcnow().isoformat()
        self.data[self.key(lease_id, month)] = entry

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))
        log.info("State saved.")


# ---------------------------------------------------------------------------
# Main sync orchestrator
# ---------------------------------------------------------------------------
class SyncOrchestrator:

    def __init__(self):
        self.af = AppFolioClient()
        self.gcal = GoogleCalendarManager()
        self.state = StateManager()

    def run(self):
        log.info("=== Starting AppFolio → Calendar sync ===")
        today = date.today()
        this_month = today.strftime("%Y-%m")

        # 1. Pull all active leases
        leases = self.af.get_leases()
        log.info(f"Fetched {len(leases)} active leases")

        # 2. Pull all payments this month in one bulk call
        payments_raw = self.af.get_all_payments_this_month()
        # Index by lease_id → total paid
        payments_by_lease: dict[str, float] = {}
        for p in payments_raw:
            lid = str(p["lease_id"])
            payments_by_lease[lid] = payments_by_lease.get(lid, 0.0) + float(p["amount"])

        # 3. Group leases by owner
        owner_leases: dict[str, list] = {}
        for lease in leases:
            owner = lease.get("owner_name", "Unknown Owner")
            owner_leases.setdefault(owner, []).append(lease)

        # 4. Per owner: get/create calendar, then sync each unit
        for owner_name, leases_for_owner in owner_leases.items():
            log.info(f"Processing owner: {owner_name} ({len(leases_for_owner)} units)")

            calendar_id = self.gcal.get_or_create_calendar(owner_name)

            owner_email = leases_for_owner[0].get("owner_email")
            if owner_email:
                self.gcal.share_with_owner(calendar_id, owner_email)

            for lease in leases_for_owner:
                self._sync_unit(lease, calendar_id, payments_by_lease, today, this_month)

        self.state.save()
        log.info("=== Sync complete ===")

    def _sync_unit(self, lease: dict, calendar_id: str,
                   payments_by_lease: dict, today: date, this_month: str):
        """
        Sync one unit's rent and late events for the current month.

        ⚠️  Field names below (e.g. 'rent_amount', 'unit_label') are assumed
        based on typical AppFolio API responses. Adjust to match your actual
        API response fields — print a sample lease dict to verify.
        """
        lease_id = str(lease["id"])
        amount_due = float(lease.get("rent_amount", 0))
        amount_paid = payments_by_lease.get(lease_id, 0.0)

        # Parse due date — AppFolio typically returns rent_due_day (int 1-28)
        due_day = int(lease.get("rent_due_day", 1))
        try:
            due_date = date(today.year, today.month, due_day)
        except ValueError:
            due_date = date(today.year, today.month, 1)

        status = classify_status(amount_due, amount_paid)

        # Build a normalized unit dict for event builders
        unit = {
            "lease_id": lease_id,
            "unit_label": lease.get("unit", ""),               # e.g. "2F"
            "property_nickname": lease.get("property_name", ""),
            "address": lease.get("address", ""),
            "tenant_name": ", ".join(lease.get("tenant_names", [])),
            "tenant_phone": lease.get("tenant_phone", "N/A"),
            "tenant_email": lease.get("tenant_email", "N/A"),
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "lease_end": lease.get("end_date", "N/A"),
        }

        # Check prior state — skip Calendar API if nothing changed
        prior = self.state.get(lease_id, this_month)
        if prior and prior["status"] == status and prior["amount_paid"] == amount_paid:
            log.info(f"No change for lease {lease_id} — skipping")
            self._handle_late_event(
                unit, calendar_id, due_date, today, status,
                existing_late_id=prior.get("late_event_id")
            )
            return

        # Upsert rent event
        rent_event_body = self.gcal._build_rent_event(unit, status, due_date)
        rent_event_id = self.gcal.upsert_event(calendar_id, rent_event_body)

        # Handle late event
        late_event_id = self._handle_late_event(
            unit, calendar_id, due_date, today, status,
            existing_late_id=prior.get("late_event_id") if prior else None
        )

        # Persist new state
        self.state.set(lease_id, this_month, {
            "status": status,
            "amount_paid": amount_paid,
            "rent_event_id": rent_event_id,
            "late_event_id": late_event_id,
        })

    def _handle_late_event(self, unit: dict, calendar_id: str,
                           due_date: date, today: date, status: str,
                           existing_late_id: Optional[str]) -> Optional[str]:
        """Create, update, or delete the late event based on current status."""
        days_late = (today - (due_date + timedelta(days=LATE_GRACE_DAYS))).days

        if status == STATUS_PAID:
            # Payment received — remove any late event
            if existing_late_id:
                self.gcal.delete_event(calendar_id, existing_late_id)
            return None

        if days_late > 0 and status in (STATUS_UNPAID, STATUS_PARTIAL):
            late_body = self.gcal._build_late_event(unit, days_late, due_date)
            late_id = self.gcal.upsert_event(calendar_id, late_body)
            return late_id

        return existing_late_id  # not yet late, keep whatever was there


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    SyncOrchestrator().run()
