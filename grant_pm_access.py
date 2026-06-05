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

from local_config import get_config, load_json_config
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SA_INFO  = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")
PM_EMAIL = str(get_config("PM_EMAIL"))   # your openkey Google account email

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

okpm = [c for c in calendars if c.get("summary", "").endswith("Portfolio")]
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
        subscribe = f"https://calendar.google.com/calendar/r?cid={cal_id.replace('@', '%40')}"
        print(f"✅  Already shared: {summary}")
        print(f"    Subscribe: {subscribe}\n")
        continue

    try:
        service.acl().insert(
            calendarId=cal_id,
            body={
                "scope": {"type": "user", "value": PM_EMAIL},
                "role": "reader",
            },
            sendNotifications=False,
        ).execute()
        print(f"✅  Granted reader access: {summary}")
        print(f"    Calendar ID: {cal_id}")

        subscribe = f"https://calendar.google.com/calendar/r?cid={cal_id.replace('@', '%40')}"
        print(f"    Subscribe:   {subscribe}\n")

    except HttpError as e:
        print(f"❌  Failed for {summary}: {e}\n")