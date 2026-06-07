"""
cleanup_duplicates.py
─────────────────────
One-time script: scans all OKPM calendars and removes duplicate events
(keeps the event ID stored in state.json, deletes extras).

Run locally or via GitHub Actions after deploying the fixed sync.py.

Usage:
    export GOOGLE_SERVICE_ACCOUNT_JSON='...'
    python cleanup_duplicates.py
"""

import json
from pathlib import Path
from collections import defaultdict
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from local_config import load_json_config

STATE_FILE      = Path("state.json")
CALENDAR_PREFIX = "OKPM"

SA_INFO = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")

creds = service_account.Credentials.from_service_account_info(
    SA_INFO,
    scopes=["https://www.googleapis.com/auth/calendar"],
)
service = build("calendar", "v3", credentials=creds)

# Load state to know which event IDs are canonical
state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
canonical_ids = set()
for key, entry in state.items():
    if key.startswith("_"):
        continue
    for field in ("status_event_id", "rent_event_id", "late_event_id"):
        eid = entry.get(field)
        if eid:
            canonical_ids.add(eid)
    for eid in entry.get("payment_event_ids", []):
        canonical_ids.add(eid)
# Also include commitment event IDs
for oid, comms in state.get("_commitments", {}).items():
    for c in comms:
        canonical_ids.add(c["event_id"])

print(f"Loaded {len(canonical_ids)} canonical event IDs from state.json\n")

# Find all OKPM calendars
calendars, page_token = [], None
while True:
    resp = service.calendarList().list(pageToken=page_token).execute()
    calendars.extend(resp.get("items", []))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break
okpm_cals = [c for c in calendars if c.get("summary", "").startswith(CALENDAR_PREFIX)]
print(f"Found {len(okpm_cals)} OKPM calendar(s)\n")

total_deleted = 0

for cal in okpm_cals:
    cal_id  = cal["id"]
    summary = cal["summary"]
    print(f"📅  {summary}")

    # Fetch ALL events (not just OKPM-tagged, to catch any orphans)
    all_events, page_token = [], None
    while True:
        resp = service.events().list(
            calendarId=cal_id,
            showDeleted=False,
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        all_events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Group OKPM events by (occupancy_id, month, event_type)
    groups = defaultdict(list)
    for ev in all_events:
        props = ev.get("extendedProperties", {}).get("private", {})
        oid   = props.get("okpm_occupancy_id")
        month = props.get("okpm_month")
        etype = props.get("okpm_event_type")
        if oid and month and etype:
            # For payment events, also group by payment_idx
            idx = props.get("okpm_payment_idx", "")
            key = (oid, month, etype, idx)
            groups[key].append(ev)

    cal_deleted = 0
    for key, events in groups.items():
        if len(events) <= 1:
            continue
        oid, month, etype, idx = key
        # Keep the canonical one (in state.json); delete extras
        keep_id = None
        for ev in events:
            if ev["id"] in canonical_ids:
                keep_id = ev["id"]
                break
        if not keep_id:
            # None in state — keep the first, delete the rest
            keep_id = events[0]["id"]

        for ev in events:
            if ev["id"] == keep_id:
                continue
            try:
                service.events().delete(
                    calendarId=cal_id, eventId=ev["id"]).execute()
                print(f"  🗑  Deleted duplicate: oid={oid} month={month} "
                      f"type={etype} id={ev['id'][:12]}…")
                cal_deleted += 1
            except HttpError as e:
                if e.resp.status != 410:
                    print(f"  ⚠️  Failed to delete {ev['id']}: {e}")

    total_deleted += cal_deleted
    if cal_deleted == 0:
        print(f"  ✅  No duplicates")
    print()

print(f"Done. Deleted {total_deleted} duplicate event(s) total.")