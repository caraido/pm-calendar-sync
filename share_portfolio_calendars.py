#!/usr/bin/env python3
r"""
One-time helper: email the PM an "Add this calendar" link for every owner
portfolio calendar.

WHY THIS EXISTS
---------------
The sync service account *owns* each "… Portfolio" calendar and shares it with
the PM via an ACL rule.  Sharing grants ACCESS, but Google does NOT automatically
drop an ACL-shared calendar into a personal Gmail account's sidebar — the PM has
to add it once.  The normal "Add" prompt arrives in the share-notification email
that Google sends when a calendar is shared *with notifications on*.  The sync
script intentionally shares with notifications OFF (to avoid spamming on every
run), so newer owners' calendars were shared silently and never showed up.

WHAT THIS DOES
--------------
For every calendar in the service account's list whose name ends in "Portfolio",
it removes the PM's existing ACL rule and re-inserts it (writer) WITH
sendNotifications=True.  That forces Google to email the PM a fresh
"<calendar> has been shared with you — Add this calendar" message for each one.

IMPORTANT
---------
Because it briefly removes-then-re-adds the PM's access, calendars you've ALREADY
added to your sidebar may disappear momentarily.  Just click the "Add" link in
each email afterward; once you've clicked all of them, every portfolio calendar
(the ones you already saw + the missing ones) will be in your sidebar.

RUN IT ONCE
-----------
Locally (Windows / Miniconda), with the same service-account JSON the sync uses:

    # Option A — JSON in an env var (same as GitHub secret):
    set GOOGLE_SERVICE_ACCOUNT_JSON=<paste the full JSON>
    set PM_EMAIL=openkey.pmcompany@gmail.com
    python share_portfolio_calendars.py

    # Option B — JSON in a file:
    set PM_EMAIL=openkey.pmcompany@gmail.com
    python share_portfolio_calendars.py path\to\service_account.json

Safe to re-run.  It only touches the PM's ACL rule on "… Portfolio" calendars.
"""

import json
import sys
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from local_config import get_config, load_json_config

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def load_service_account() -> dict:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            return json.load(f)
    return load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")


def list_portfolio_calendars(svc) -> list[tuple[str, str]]:
    cals, token = [], None
    while True:
        resp = svc.calendarList().list(pageToken=token).execute()
        for c in resp.get("items", []):
            summary = (c.get("summary") or "").strip()
            if summary.endswith("Portfolio"):
                cals.append((c["id"], summary))
        token = resp.get("nextPageToken")
        if not token:
            break
    return sorted(cals, key=lambda x: x[1].lower())


def reshare_with_notification(svc, calendar_id: str, pm_email: str):
    # Remove the PM's existing rule (if any) so the re-insert sends a fresh email.
    acl = svc.acl().list(calendarId=calendar_id).execute()
    for rule in acl.get("items", []):
        if rule.get("scope", {}).get("value") == pm_email:
            svc.acl().delete(calendarId=calendar_id, ruleId=rule["id"]).execute()
            break
    svc.acl().insert(
        calendarId=calendar_id,
        body={"scope": {"type": "user", "value": pm_email}, "role": "owner"},
        sendNotifications=True,
    ).execute()


def main():
    pm_email = str(get_config("PM_EMAIL", "openkey.pmcompany@gmail.com")).strip()
    info = load_service_account()
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    svc = build("calendar", "v3", credentials=creds)

    cals = list_portfolio_calendars(svc)
    if not cals:
        print("No '… Portfolio' calendars found in the service account's list.")
        return

    print(f"Found {len(cals)} portfolio calendars.")
    print(f"Re-sharing each with {pm_email} (notification ON) to trigger an "
          f"'Add this calendar' email…\n")

    ok = 0
    for cid, summary in cals:
        try:
            reshare_with_notification(svc, cid, pm_email)
            print(f"  ✅ {summary}")
            ok += 1
            time.sleep(0.3)  # gentle on the ACL-notification quota
        except HttpError as e:
            print(f"  ❌ {summary}: {e}")

    print(f"\nDone — {ok}/{len(cals)} re-shared.")
    print(f"Check the inbox for {pm_email}. Each message has an 'Add this "
          f"calendar' button; click them all and every portfolio calendar will "
          f"appear in your sidebar.")


if __name__ == "__main__":
    main()