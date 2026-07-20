"""
clean_keep_commitments.py
─────────────────────────
Smart cleanup that PRESERVES your dragged promises.

WHAT IT DOES, per sync-managed calendar (from state.json `_calendars` —
since the group cutover these are the property-group calendars):
  1. KEEPS every commitment event (your dragged promises) — these are the
     source of truth for promises.
  2. DEDUPES commitment events: if several sit on the same date for the same
     unit (from the duplication bug), keeps ONE and deletes the rest.
  3. DELETES all other events — status, payment, rent placeholders, and any
     untagged stale events from old code versions.

Then WIPES state.json.  The next sync (with the always-rediscover sync.py)
rebuilds all status/payment/placeholder events fresh from AppFolio AND
re-discovers your surviving commitment events from the calendars.

⚠️  WHAT YOU KEEP: every promise you dragged (deduplicated).
⚠️  WHAT YOU LOSE: nothing of value — only stale/duplicate/auto-generated events
    that the sync recreates correctly on the next run.

PROCEDURE:
  1. DISABLE the GitHub Actions workflow (Actions → ⋯ → Disable)
  2. python clean_keep_commitments.py        (type YES to confirm)
  3. Deploy the new sync.py
  4. git add state.json sync.py
     git commit -m "fix: clean rebuild preserving commitments"
     git push
  5. RE-ENABLE the workflow → Run workflow.  Run it TWICE:
       • Run 1 rebuilds status/payment/placeholders + rediscovers promises
       • Run 2 sweeps: suppresses the 1st-of-month event for promised units
  6. Verify.

Requires confirmation (type YES) before deleting.
"""

import json, os, sys, time
from pathlib import Path
from collections import defaultdict
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from local_config import load_json_config

SA_INFO = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")

creds = service_account.Credentials.from_service_account_info(
    SA_INFO, scopes=["https://www.googleapis.com/auth/calendar"])
svc = build("calendar", "v3", credentials=creds)

def _delete_with_retry(cal_id, event_id, retries=4, base=4):
    """Delete an event, retrying with backoff on 403/429/500/503."""
    for attempt in range(retries):
        try:
            svc.events().delete(calendarId=cal_id, eventId=event_id).execute()
            return True
        except HttpError as e:
            if e.resp.status in (404, 410):
                return True  # already gone
            if e.resp.status in (403, 429, 500, 503) and attempt < retries - 1:
                time.sleep(base * (2 ** attempt))
                continue
            print(f"    ⚠️ failed: {event_id}: {e}")
            return False
    return False
 
# ── Find all sync-managed calendars ──────────────────────────────────────
# Since the group cutover, managed calendars are the values of state.json's
# `_calendars` map (group-scoped) — NOT a "Portfolio" summary suffix, which
# now matches only the retired legacy owner calendars.
_state_path = "state.json" if os.path.exists("state.json") else "../state.json"
_managed_ids: set = set()
if os.path.exists(_state_path):
    with open(_state_path, encoding="utf-8-sig") as _f:
        _managed_ids = set((json.load(_f).get("_calendars") or {}).values())
cals, token = [], None
while True:
    resp = svc.calendarList().list(pageToken=token).execute()
    for c in resp.get("items", []):
        if c["id"] in _managed_ids:
            cals.append((c["id"], c["summary"]))
    token = resp.get("nextPageToken")
    if not token:
        break

print(f"Found {len(cals)} managed calendar(s) (from state.json _calendars).\n")
 
# ── First pass: count + categorize (read-only preview) ───────────────────
def fetch_all(cal_id):
    evs, tok = [], None
    while True:
        resp = svc.events().list(
            calendarId=cal_id, showDeleted=False,
            maxResults=2500, pageToken=tok).execute()
        evs.extend(resp.get("items", []))
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return evs
 
def categorize(events):
    commitments, others = [], []
    for ev in events:
        etype = (ev.get("extendedProperties", {})
                 .get("private", {}).get("okpm_event_type"))
        (commitments if etype == "commitment" else others).append(ev)
    return commitments, others
 
print("Scanning (read-only preview)...")
preview = {}
tot_keep = tot_dedupe = tot_delete = 0
for cal_id, summary in cals:
    events = fetch_all(cal_id)
    commitments, others = categorize(events)
 
    # Dedupe commitments by (occupancy_id, start_date)
    seen, keep, dupes = set(), [], []
    for ev in commitments:
        oid = ev.get("extendedProperties", {}).get("private", {}).get("okpm_occupancy_id")
        start = ev.get("start", {}).get("date") or ev.get("start", {}).get("dateTime", "")[:10]
        key = (oid, start)
        if key in seen:
            dupes.append(ev)
        else:
            seen.add(key)
            keep.append(ev)
 
    preview[cal_id] = {"keep": keep, "dupes": dupes, "others": others,
                       "summary": summary}
    tot_keep   += len(keep)
    tot_dedupe += len(dupes)
    tot_delete += len(others)
    print(f"  {summary}: keep {len(keep)} promise(s), "
          f"dedupe {len(dupes)}, delete {len(others)} other")
 
print(f"\n{'═' * 55}")
print(f"SUMMARY")
print(f"{'═' * 55}")
print(f"  Promise events KEPT:          {tot_keep}")
print(f"  Duplicate promises removed:   {tot_dedupe}")
print(f"  Other events deleted:         {tot_delete}")
print(f"  (status/payment/placeholder/stale — sync rebuilds these)")
 
confirm = input("\nType YES to proceed: ").strip()
if confirm != "YES":
    print("Aborted. Nothing deleted.")
    sys.exit(0)
 
# ── Second pass: delete ──────────────────────────────────────────────────
deleted = 0
for cal_id, info in preview.items():
    print(f"\n📅 {info['summary']}")
    to_delete = info["dupes"] + info["others"]
    for ev in to_delete:
        if _delete_with_retry(cal_id, ev["id"]):
            deleted += 1
            if deleted % 25 == 0:
                print(f"    deleted {deleted}...")
                time.sleep(0.15)  # gentle pacing on rate limits
    print(f"    kept {len(info['keep'])} promise(s), "
          f"removed {len(to_delete)} event(s)")
 
print(f"\n🗑  Deleted {deleted} events ({tot_dedupe} dup promises + "
      f"{tot_delete} others). Kept {tot_keep} promises.")
 
# ── Wipe state.json (always-rediscover sync will rebuild) ────────────────
Path("state.json").write_text(json.dumps({"_commitments": {}}, indent=2))
print(f"💾 state.json wiped.")
 
print(f"\n✅ Clean slate (promises preserved).")
print(f"   Next: deploy sync.py, commit, push, re-enable workflow, run TWICE.")
 