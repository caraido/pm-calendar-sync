"""
rollback_group_cutover.py
─────────────────────────
Rolls back the calendar side of the July-2026 group cutover: strips the
"[RETIRED] " prefix off every legacy owner calendar recorded in state.json's
`_retired_calendars`, and (optionally) re-shares each with its owner's email
from the cached owner directory.

Use together with a code revert:
  1. git revert the group-cutover commit (or check out the pre-cutover tree)
  2. git checkout <pre-cutover-sha> -- state.json && git commit
     (git history is the archive of the purged owner-scoped keys)
  3. python misc/rollback_group_cutover.py          (add --share to re-share owners)
  4. Push BEFORE the next nightly run — otherwise the reverted sync won't
     find "{Owner} Portfolio" by name and will create empty duplicates.

The 9 group calendars are left in place (harmless once the code is
reverted; delete them by hand if desired).

Reads `_retired_calendars` from the CURRENT state.json — run this BEFORE
step 2 replaces state.json, or point STATE_PATH at a copy that has it.
"""
import argparse
import json
import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from local_config import load_json_config

RETIRED_PREFIX = "[RETIRED] "
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
CACHE_DIRECTORIES = "cache/directories.json"


def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--share", action="store_true",
                    help="also re-share each calendar with its owner's email "
                         "(reader) from the cached owner directory")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen without touching Google")
    args = ap.parse_args()

    if not os.path.exists(STATE_PATH):
        sys.exit(f"{STATE_PATH} not found — run from the repo root")
    state = _read_json(STATE_PATH)
    retired = state.get("_retired_calendars") or {}
    if not retired:
        sys.exit("state.json has no _retired_calendars — nothing to roll back")

    # owner_id → email, if a pre-cutover directories cache is available.
    owner_emails = {}
    if args.share and os.path.exists(CACHE_DIRECTORIES):
        cached = _read_json(CACHE_DIRECTORIES)
        for o in cached.get("owners") or []:   # pre-cutover cache shape
            if o.get("owner_id") is not None and o.get("email"):
                owner_emails[str(o["owner_id"])] = o["email"].strip()
        if not owner_emails:
            print("⚠️  cache/directories.json has no 'owners' rows (post-cutover "
                  "shape) — cannot re-share; restore a pre-cutover cache first")

    sa = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/calendar"])
    svc = build("calendar", "v3", credentials=creds)

    done = set()
    for owner_id, cal_id in retired.items():
        email = owner_emails.get(str(owner_id), "")
        if cal_id in done:
            pass  # shared calendar (several owners): renamed once, share each
        else:
            done.add(cal_id)
            try:
                cal = svc.calendars().get(calendarId=cal_id).execute()
            except HttpError as e:
                print(f"✗ {cal_id}: {e.resp.status} — skipping")
                continue
            summary = cal.get("summary", "")
            if summary.startswith(RETIRED_PREFIX):
                new_summary = summary[len(RETIRED_PREFIX):]
                print(f"→ {summary!r} → {new_summary!r}")
                if not args.dry_run:
                    svc.calendars().patch(
                        calendarId=cal_id,
                        body={"summary": new_summary}).execute()
            else:
                print(f"= {summary!r} (no prefix — already rolled back)")
        if args.share and email:
            print(f"  + share (reader) with {email}")
            if not args.dry_run:
                try:
                    svc.acl().insert(
                        calendarId=cal_id,
                        body={"scope": {"type": "user", "value": email},
                              "role": "reader"},
                        sendNotifications=True).execute()
                except HttpError as e:
                    print(f"  ✗ could not share with {email}: {e.resp.status}")
    print(f"\nDone: {len(done)} calendar(s) processed"
          + (" (dry run — nothing changed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
