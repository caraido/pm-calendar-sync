"""
diagnose_calendar.py
────────────────────
Dumps EVERY event on a sync-managed calendar (by group-name substring) with
its okpm tags, start date, and title.  Reveals stale events, duplicates,
wrong dates, and untagged orphans.

Since the group cutover, managed calendars come from state.json's
`_calendars` map (one per property group).

Usage:
    set GOOGLE_SERVICE_ACCOUNT_JSON=...   (or path to json)
    python probe_matching.py Midwest
    python probe_matching.py "Tian Xin"
"""

import json, os, sys
from collections import defaultdict
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

from local_config import load_json_config

NAME_FILTER = sys.argv[1] if len(sys.argv) > 1 else ""

SA_INFO = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")

creds = service_account.Credentials.from_service_account_info(
    SA_INFO, scopes=["https://www.googleapis.com/auth/calendar"])
svc = build("calendar", "v3", credentials=creds)

# Find matching sync-managed calendars (state.json `_calendars` values)
state_path = "state.json" if os.path.exists("state.json") else "../state.json"
managed_ids: set = set()
if os.path.exists(state_path):
    with open(state_path, encoding="utf-8-sig") as f:
        managed_ids = set((json.load(f).get("_calendars") or {}).values())

cals, token = [], None
while True:
    resp = svc.calendarList().list(pageToken=token).execute()
    for c in resp.get("items", []):
        summary = (c.get("summary") or "")
        if c["id"] in managed_ids and NAME_FILTER.lower() in summary.lower():
            cals.append((c["id"], summary))
    token = resp.get("nextPageToken")
    if not token:
        break

print(f"Matched {len(cals)} calendar(s) for filter '{NAME_FILTER}'\n")

# Load state for cross-reference
state_path = Path("state.json")
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

for cal_id, summary in cals:
    print("=" * 70)
    print(f"CALENDAR: {summary}")
    print(f"  id: {cal_id}")
    print("=" * 70)

    # Fetch ALL events
    events, token = [], None
    while True:
        resp = svc.events().list(
            calendarId=cal_id, showDeleted=False,
            maxResults=2500, pageToken=token,
            timeMin="2026-05-01T00:00:00Z",
            timeMax="2026-08-01T00:00:00Z",
        ).execute()
        events.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            break

    print(f"\nTotal events (May–Jul): {len(events)}\n")

    # Group by occupancy_id
    by_oid = defaultdict(list)
    untagged = []
    for ev in events:
        props = ev.get("extendedProperties", {}).get("private", {})
        oid = props.get("okpm_occupancy_id")
        start = ev.get("start", {}).get("date") or ev.get("start", {}).get("dateTime", "")[:10]
        etype = props.get("okpm_event_type", "?")
        month = props.get("okpm_month", "?")
        title = ev.get("summary", "")[:45]
        if oid:
            by_oid[oid].append((start, etype, month, title, ev["id"][:12]))
        else:
            untagged.append((start, title, ev["id"][:12]))

    for oid in sorted(by_oid.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        entries = by_oid[oid]
        flag = "  ⚠️ DUPLICATE" if len(entries) > 1 and \
               len([e for e in entries if e[1] == "status"]) > 1 else ""
        print(f"  oid={oid} ({len(entries)} event(s)){flag}")
        for start, etype, month, title, eid in sorted(entries):
            print(f"      {start}  [{etype:10s}] month={month}  {title}  ({eid})")

    if untagged:
        print(f"\n  ⚠️ UNTAGGED events (stale from old code): {len(untagged)}")
        for start, title, eid in sorted(untagged):
            print(f"      {start}  {title}  ({eid})")

    print()