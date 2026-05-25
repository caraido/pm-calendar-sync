"""
list_calendars.py
─────────────────
One-time helper: lists all OKPM-managed calendars with their IDs
and shareable subscription links.

Run locally:
    GOOGLE_SERVICE_ACCOUNT_JSON='...' python list_calendars.py
"""

import os, json
from google.oauth2 import service_account
from googleapiclient.discovery import build
_sa_raw  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
PM_EMAIL = os.environ["PM_EMAIL"]   # your openkey Google account email
if _sa_raw.strip().endswith(".json") or ("\\" in _sa_raw or "/" in _sa_raw):
    # Looks like a file path — read it
    with open(_sa_raw.strip()) as f:
        SA_INFO = json.load(f)
else:
    SA_INFO = json.loads(_sa_raw)

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

okpm = [c for c in calendars if c.get("summary", "").startswith("OKPM")]

print(f"\nFound {len(okpm)} OKPM calendar(s):\n")
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
