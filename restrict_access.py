"""
restrict_access.py
──────────────────
One-time script: revokes calendar access from everyone except
the PM account and a single specified owner email.

Usage (Windows):
    set GOOGLE_SERVICE_ACCOUNT_JSON=D:\path\to\service-account.json
    set PM_EMAIL=your-openkey@gmail.com
    set KEEP_EMAIL=ry.d.palmer@gmail.com
    python restrict_access.py
"""

import os, json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_sa_raw  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
PM_EMAIL = os.environ["PM_EMAIL"]
KEEP_EMAIL = os.environ["KEEP_EMAIL"]   # the one owner to keep

if _sa_raw.strip().endswith(".json") or ("\\" in _sa_raw or "/" in _sa_raw):
    with open(_sa_raw.strip()) as f:
        SA_INFO = json.load(f)
else:
    SA_INFO = json.loads(_sa_raw)

creds = service_account.Credentials.from_service_account_info(
    SA_INFO, scopes=["https://www.googleapis.com/auth/calendar"]
)
service = build("calendar", "v3", credentials=creds)

# Emails that should NEVER be revoked
PROTECTED = {PM_EMAIL.lower(), KEEP_EMAIL.lower()}

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
    print(f"📅  {summary}")

    acl = service.acl().list(calendarId=cal_id).execute()
    for rule in acl.get("items", []):
        scope = rule.get("scope", {})
        role  = rule.get("role", "")
        email = scope.get("value", "").lower()
        stype = scope.get("type", "")

        # Never touch the service account owner rule or default rules
        if stype in ("default", "domain") or role == "owner" and email == "":
            continue
        # Never touch the service account itself
        if "iam.gserviceaccount.com" in email:
            continue
        # Never touch the calendar's own ACL entry
        if "group.calendar.google.com" in email:
            continue
        # Keep protected emails
        if email in PROTECTED:
            print(f"  ✅  Keeping  {email} ({role})")
            continue
        # Revoke everyone else
        try:
            service.acl().delete(calendarId=cal_id, ruleId=rule["id"]).execute()
            print(f"  🚫  Revoked  {email} ({role})")
        except HttpError as e:
            print(f"  ⚠️   Failed to revoke {email}: {e}")

    # Ensure Ryan Palmer specifically has reader access on all calendars
    acl_refresh = service.acl().list(calendarId=cal_id).execute()
    has_keep = any(
        r["scope"].get("value", "").lower() == KEEP_EMAIL.lower()
        for r in acl_refresh.get("items", [])
    )
    if not has_keep:
        service.acl().insert(
            calendarId=cal_id,
            body={
                "scope": {"type": "user", "value": KEEP_EMAIL},
                "role": "reader",
            },
            sendNotifications=False,
        ).execute()
        print(f"  ➕  Granted reader access to {KEEP_EMAIL}")

    print()

print("Done.")