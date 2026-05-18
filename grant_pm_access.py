"""
grant_pm_access.py
──────────────────
One-time script: grants your PM Google account full owner access
to all OKPM-managed calendars.

Run once locally, then never again (sync.py handles it going forward).

Usage:
    GOOGLE_SERVICE_ACCOUNT_JSON='...' \
    PM_EMAIL='your-openkey-account@gmail.com' \
    python grant_pm_access.py
"""

import os, json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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

# Get all OKPM calendars
calendars, page_token = [], None
while True:
    resp = service.calendarList().list(pageToken=page_token).execute()
    calendars.extend(resp.get("items", []))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

okpm = [c for c in calendars if c.get("summary", "").startswith("OKPM")]
print(f"Found {len(okpm)} OKPM calendar(s)\n")

for cal in okpm:
    cal_id  = cal["id"]
    summary = cal["summary"]

    # Check if already shared
    acl = service.acl().list(calendarId=cal_id).execute()
    already = any(
        r["scope"].get("value") == PM_EMAIL
        for r in acl.get("items", [])
    )
    if already:
        print(f"✅  Already shared: {summary}")
        continue

    try:
        service.acl().insert(
            calendarId=cal_id,
            body={
                "scope": {"type": "user", "value": PM_EMAIL},
                "role": "owner",   # PM gets full control, owners get reader
            },
            sendNotifications=False,  # no email spam to yourself
        ).execute()
        print(f"✅  Granted owner access: {summary}")
        print(f"    Calendar ID: {cal_id}")

        subscribe = f"https://calendar.google.com/calendar/r?cid={cal_id.replace('@', '%40')}"
        print(f"    Subscribe:   {subscribe}\n")

    except HttpError as e:
        print(f"❌  Failed for {summary}: {e}\n")
