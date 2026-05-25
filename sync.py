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

LATE_GRACE_DAYS  = int(os.environ.get("LATE_GRACE_DAYS", 5))
RENT_DUE_DAY     = int(os.environ.get("RENT_DUE_DAY", 1))        # day of month rent is due
PM_EMAIL         = os.environ.get("PM_EMAIL", "")                 # your openkey PM Google account
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
STATUS_PARTIAL = "🟡 Partial"
STATUS_UNPAID  = "🔴 Unpaid"
STATUS_LATE    = "⚠️ Late"


def classify_status(rent: float, past_due: float) -> str:
    """
    Classify payment status using rent_roll fields directly.

    past_due = 0          → fully paid (or rent not yet charged)
    0 < past_due < rent   → partial payment received
    past_due >= rent      → no payment received this month
    """
    if past_due <= 0:
        return STATUS_PAID
    elif past_due < rent:
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


def format_address(row: dict) -> str:
    parts = [
        row.get("property_street", ""),
        row.get("property_city", ""),
        row.get("property_state", ""),
        row.get("property_zip", "") or "",
    ]
    return ", ".join(p for p in parts if p)


def unit_label(row: dict) -> str:
    """Return 'Unit X' or empty string for single-family."""
    u = row.get("unit")
    return f"Unit {u}" if u else ""


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
        balance = unit["past_due"]

        # Title: status · unit label · property name · rent amount
        unit_part = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        title = (
            f"{status} · {unit_part}"
            f"{unit['property_name']} · ${unit['rent']:,.0f}"
        )

        tenants = unit['tenant']
        if unit.get('additional_tenants'):
            tenants += f", {unit['additional_tenants']}"

        description = "\n".join([
            f"Tenant(s):    {tenants}",
            (f"Unit:         {unit['unit_label']}  |  " if unit['unit_label'] else "") +
            f"{unit['address']}",
            "─" * 40,
            f"Monthly Rent: ${unit['rent']:,.2f}",
            f"Past Due:     ${balance:,.2f}",
            f"Status:       {status}",
            "─" * 40,
            f"Late After:   {late_threshold.strftime('%b %d, %Y')}",
            f"Lease:        {unit['lease_from']} → {unit['lease_to']}",
        ])

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

    def _build_late_event(self, unit: dict, days_late: int) -> dict:
        today_str = date.today().isoformat()
        unit_part = f"{unit['unit_label']} · " if unit['unit_label'] else ""
        title = (
            f"⚠️ Late Day {days_late} · {unit_part}"
            f"{unit['property_name']} · ${unit['past_due']:,.0f} owed"
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

        # ── 2. Build property_id → owner lookup ─────────────────────────────
        prop_to_owner = build_owner_property_map(owners)
        log.info(f"  {len(prop_to_owner)} property→owner mappings")

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
                self._sync_unit(row, calendar_id, due_date, today, this_month)

        # ── 6. Persist state ─────────────────────────────────────────────────
        self.state.save()
        log.info("=== Sync complete ===")

    def _sync_unit(
        self, row: dict, calendar_id: str,
        due_date: date, today: date, this_month: str
    ):
        occupancy_id = str(row["occupancy_id"])
        rent         = float(row.get("rent", 0) or 0)
        past_due     = float(row.get("past_due", 0) or 0)
        status       = classify_status(rent, past_due)

        # Normalized unit dict passed to event builders
        unit = {
            "occupancy_id":      occupancy_id,
            "property_name":     row.get("property_name", ""),
            "address":           format_address(row),
            "unit_label":        unit_label(row),
            "tenant":            row.get("tenant", ""),
            "additional_tenants": row.get("additional_tenants", ""),
            "rent":              rent,
            "past_due":          past_due,
            "lease_from":        row.get("lease_from", ""),
            "lease_to":          row.get("lease_to", ""),
        }

        # Skip Calendar API if nothing changed since last run
        prior = self.state.get(occupancy_id, this_month)
        if prior and prior["status"] == status and prior["past_due"] == past_due:
            log.info(f"  No change for occupancy {occupancy_id} — skipping")
            # Still handle late event — day count changes even if status is the same
            self._handle_late_event(
                unit, calendar_id, due_date, today, status,
                existing_late_id=prior.get("late_event_id"),
            )
            return

        # Upsert the rent event
        rent_body    = self.gcal._build_rent_event(unit, status, due_date)
        rent_event_id = self.gcal.upsert_event(calendar_id, rent_body)

        # Upsert or delete the late event
        late_event_id = self._handle_late_event(
            unit, calendar_id, due_date, today, status,
            existing_late_id=prior.get("late_event_id") if prior else None,
        )

        self.state.set(occupancy_id, this_month, {
            "status":        status,
            "past_due":      past_due,
            "rent_event_id": rent_event_id,
            "late_event_id": late_event_id,
        })

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