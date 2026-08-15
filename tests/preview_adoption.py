"""
preview_adoption.py
───────────────────
READ-ONLY dry run of the untagged-copy adoption scan: lists every untagged
event on every sync-managed group calendar, runs the same classifier
(`transforms.classify_sync_copy`) and the same tenant/address attribution
the adopter uses, and prints per event what a real run WOULD do — adopt as
which promise kind, or skip with which reason.  Zero writes.

Usage:
    python tests/preview_adoption.py
"""

import glob
import json, os, sys
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Runnable both as `python tests/preview_adoption.py` (repo root) and from
# tests/ — local_config.py lives in the repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from local_config import load_json_config

# Resolve the service-account credentials FIRST (before the package import
# stubs any env var): local_config chain, falling back to a service-account
# key file dropped in secrets/.
try:
    SA_INFO = load_json_config("GOOGLE_SERVICE_ACCOUNT_JSON")
except KeyError:
    SA_INFO = None
    for cand in glob.glob(os.path.join(REPO_ROOT, "secrets", "*.json")):
        try:
            data = json.loads(Path(cand).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "service_account":
            SA_INFO = data
            break
    if SA_INFO is None:
        sys.exit("No Google service-account credentials found "
                 "(local_config / env / secrets/*.json)")

# pm_calendar_sync.config reads its env vars at import time — stub the
# AppFolio ones (never called here) and feed it the resolved credentials.
os.environ.setdefault("APPFOLIO_DB_NAME", "offline")
os.environ.setdefault("APPFOLIO_CLIENT_ID", "offline")
os.environ.setdefault("APPFOLIO_CLIENT_SECRET", "offline")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(SA_INFO))
from pm_calendar_sync.transforms import (          # noqa: E402
    classify_sync_copy, normalize_tenant_name, format_address, unit_label,
    active_rows,
)

creds = service_account.Credentials.from_service_account_info(
    SA_INFO, scopes=["https://www.googleapis.com/auth/calendar"])
svc = build("calendar", "v3", credentials=creds, cache_discovery=False)


def load_json_file(path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {}
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            continue
    return {}


state = load_json_file(Path(REPO_ROOT) / "state.json")
cals = state.get("_calendars") or {}
roll = (load_json_file(Path(REPO_ROOT) / "cache" / "rent_roll.json")
        or {}).get("rows") or []
prop_groups = (load_json_file(
    Path(REPO_ROOT) / "cache" / "directories.json")
    or {}).get("property_groups") or []

# property_id → {"g{gid}", ...} so rows can be bucketed per calendar scope.
pid_to_scopes: dict = {}
for row in prop_groups:
    gid, pid = row.get("property_group_id"), row.get("property_id")
    if gid in (None, "") or pid in (None, ""):
        continue
    pid_to_scopes.setdefault(str(pid), set()).add(f"g{str(gid).strip()}")

rows_by_scope: dict = {}
for r in active_rows(roll):
    for scope in pid_to_scopes.get(str(r.get("property_id")), set()):
        rows_by_scope.setdefault(scope, []).append(r)

total = would_adopt = 0
for scope, cal_id in sorted(cals.items()):
    rows = rows_by_scope.get(scope, [])
    header_shown = False
    page = None
    while True:
        resp = svc.events().list(calendarId=cal_id, showDeleted=False,
                                 maxResults=2500, pageToken=page).execute()
        for ev in resp.get("items", []):
            props = ev.get("extendedProperties", {}).get("private", {})
            if props.get("okpm_occupancy_id"):
                continue
            total += 1
            if not header_shown:
                print(f"\n=== {scope}  ({cal_id[:20]}…) ===")
                header_shown = True
            summary = ev.get("summary") or ""
            desc = ev.get("description") or ""
            start = ev.get("start", {})
            anchor = start.get("date") or start.get("dateTime", "")[:10]
            info = classify_sync_copy(summary, desc)
            line = f"  {anchor or '????-??-??'}  {summary[:64]!r}"
            if info is None:
                print(f"{line}\n      -> IGNORED (not sync-styled)")
                continue
            kind = info["kind"]
            tenant = info["tenant"]
            if kind == "commitment":
                print(f"{line}\n      -> commitment copy "
                      f"(existing adoption path)")
                would_adopt += 1
                continue
            matches = [r for r in rows
                       if normalize_tenant_name(r.get("tenant", ""))
                       == tenant]
            if len(matches) > 1:
                matches = [r for r in matches
                           if format_address(r) in desc
                           and (not unit_label(r)
                                or unit_label(r) in desc)]
            if not tenant:
                verdict = "SKIP (no tenant recoverable)"
            elif kind == "placeholder" and not info["late_after_month"]:
                verdict = "SKIP (Late After month unparseable)"
            elif len(matches) != 1:
                verdict = (f"SKIP (tenant {tenant!r} matched "
                           f"{len(matches)} unit(s))")
            else:
                oid = matches[0].get("occupancy_id")
                tgt = (f"kickstart for {info['late_after_month']}"
                       if kind == "placeholder"
                       else f"promise (source "
                            f"{info['source_type']}) at {anchor}")
                verdict = f"ADOPT as {tgt}  [oid {oid}]"
                would_adopt += 1
            print(f"{line}\n      kind={kind:14s} tenant={tenant!r}\n"
                  f"      -> {verdict}")
        page = resp.get("nextPageToken")
        if not page:
            break

print(f"\n{total} untagged event(s) across {len(cals)} calendars; "
      f"{would_adopt} would be adopted. (Dry run — nothing was written.)")
