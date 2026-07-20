"""
list_calendars.py
─────────────────
One-time helper: lists all OKPM-managed calendars with their IDs
and shareable subscription links.

Since the group cutover, managed calendars come from state.json's
`_calendars` map (one per property group); retired owner calendars
("[RETIRED] … Portfolio") are listed separately.

Run locally:
    GOOGLE_SERVICE_ACCOUNT_JSON='...' python list_calendars.py
"""
import json
import os

from local_config import load_json_config
from google.oauth2 import service_account
from googleapiclient.discovery import build

SA_INFO = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")

creds = service_account.Credentials.from_service_account_info(
    SA_INFO,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
service = build("calendar", "v3", credentials=creds)

# List all calendars the service account owns
calendars = []
page_token = None
while True:
    resp = service.calendarList().list(pageToken=page_token).execute()
    calendars.extend(resp.get("items", []))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

state_path = "state.json" if os.path.exists("state.json") else "../state.json"
managed_ids = set()
if os.path.exists(state_path):
    with open(state_path, encoding="utf-8-sig") as f:
        managed_ids = set((json.load(f).get("_calendars") or {}).values())

okpm    = [c for c in calendars if c["id"] in managed_ids]
retired = [c for c in calendars
           if c.get("summary", "").startswith("[RETIRED] ")]

print(f"\nFound {len(okpm)} managed group calendar(s) "
      f"(+ {len(retired)} retired owner calendar(s)):\n")
for cal in okpm:
    cal_id  = cal["id"]
    summary = cal["summary"]

    # Get ACL to show who it's shared with
    acl = service.acl().list(calendarId=cal_id).execute()
    readers = [
        r["scope"]["value"]
        for r in acl.get("items", [])
        if r.get("role") == "reader" and r["scope"]["type"] == "user"
    ]

    # Google Calendar subscription link (works for any Google account)
    subscribe_link = (
        f"https://calendar.google.com/calendar/r?cid="
        f"{cal_id.replace('@', '%40')}"
    )

    print(f"📅  {summary}")
    print(f"    ID:        {cal_id}")
    print(f"    Shared with: {', '.join(readers) if readers else 'nobody yet'}")
    print(f"    Subscribe: {subscribe_link}")
    print()
