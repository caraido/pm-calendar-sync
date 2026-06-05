"""
probe_matching.py
─────────────────
Two-part diagnostic:

PART A — AppFolio tenant_ledger field inspection
  Dumps EVERY field name from a raw tenant_ledger row to see if there's an
  occupancy_id, unit, or property field we can key on instead of payer name.
  Also searches for specific tenants (Luis Ramos, Dennis Washington, Dellissia).

PART B — state.json commitment audit
  Checks for phantom commitments that could be triggering suppress_kickstart
  and deleting status events for tenants who shouldn't have promises.

Usage (Windows / Miniconda):
    set APPFOLIO_DB_NAME=openkey
    set APPFOLIO_CLIENT_ID=xxx
    set APPFOLIO_CLIENT_SECRET=xxx
    python probe_matching.py
"""

import json, requests
from datetime import date
from pathlib import Path

from local_config import get_config

DB  = get_config("APPFOLIO_DB_NAME")
CID = get_config("APPFOLIO_CLIENT_ID")
CSC = get_config("APPFOLIO_CLIENT_SECRET")
BASE    = f"https://{CID}:{CSC}@{DB}.appfolio.com/api/v2/reports"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

today = date.today()
from_date = today.replace(day=1).isoformat()
to_date   = today.isoformat()

# ═══════════════════════════════════════════════════════════════════════════
# PART A — Inspect tenant_ledger fields
# ═══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("PART A: TENANT LEDGER — FULL FIELD INSPECTION")
print("=" * 70)
 
print(f"\nPulling tenant_ledger from {from_date} to {to_date}...")
r = requests.post(
    f"{BASE}/tenant_ledger.json",
    headers=HEADERS,
    json={"from_date": from_date, "to_date": to_date},
    timeout=30,
)
if r.status_code != 200:
    print(f"ERROR: {r.status_code}: {r.text[:300]}")
    exit(1)
 
ledger_rows = r.json().get("results", [])
print(f"Total ledger rows: {len(ledger_rows)}")
 
# Show ALL field names from the first row
if ledger_rows:
    print(f"\n{'─' * 50}")
    print("ALL FIELDS in a tenant_ledger row:")
    print(f"{'─' * 50}")
    first = ledger_rows[0]
    for key in sorted(first.keys()):
        val = first[key]
        print(f"  {key:30s} = {repr(val)[:80]}")
 
    # Check specifically for occupancy/unit/property-like fields
    print(f"\n{'─' * 50}")
    print("FIELDS CONTAINING 'occupancy', 'unit', 'property', 'tenant', 'id':")
    print(f"{'─' * 50}")
    id_fields = [k for k in first.keys()
                 if any(x in k.lower() for x in
                        ("occupancy", "unit", "property", "tenant", "id",
                         "lease", "address"))]
    if id_fields:
        for key in id_fields:
            print(f"  ★ {key:30s} = {repr(first[key])}")
    else:
        print("  (none found — name-matching is the only option)")
 
# ── Search for specific tenants ──────────────────────────────────────────
SEARCH_NAMES = ["luis", "ramos", "dennis", "washington", "dellissia"]
 
print(f"\n{'─' * 50}")
print("SEARCHING LEDGER for: " + ", ".join(SEARCH_NAMES))
print(f"{'─' * 50}")
 
credit_rows = [row for row in ledger_rows
               if float(row.get("credit") or 0) > 0]
 
for name in SEARCH_NAMES:
    matches = [
        row for row in credit_rows
        if name in (row.get("payer") or "").lower()
        or name in (row.get("description") or "").lower()
        or name in json.dumps(row).lower()
    ]
    if matches:
        print(f"\n  Found {len(matches)} row(s) matching '{name}':")
        for row in matches[:3]:
            print(f"    {json.dumps(row, indent=6, default=str)}")
    else:
        print(f"\n  '{name}' — NO matches in credit rows")
 
# ── Also dump ALL unique payer names alongside rent_roll tenant names ────
print(f"\n{'─' * 50}")
print("ALL UNIQUE PAYER NAMES in tenant_ledger (credit > 0):")
print(f"{'─' * 50}")
payers = sorted(set(row.get("payer", "?") for row in credit_rows))
for p in payers:
    print(f"  {p}")
 
# ═══════════════════════════════════════════════════════════════════════════
# PART B — rent_roll: find occupancy IDs for target tenants
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 70}")
print("PART B: RENT ROLL — OCCUPANCY IDs FOR TARGET TENANTS")
print("=" * 70)
 
print("Pulling rent_roll...")
r2 = requests.post(f"{BASE}/rent_roll.json", headers=HEADERS, json={}, timeout=30)
rent_rows = r2.json().get("results", [])
print(f"Total rent_roll rows: {len(rent_rows)}")
 
for name in ["luis", "ramos", "dennis", "washington", "dellissia"]:
    matches = [
        row for row in rent_rows
        if name in (row.get("tenant") or "").lower()
    ]
    if matches:
        for row in matches:
            print(f"\n  ★ {row.get('tenant')}")
            print(f"    occupancy_id:  {row.get('occupancy_id')}")
            print(f"    property_id:   {row.get('property_id')}")
            print(f"    property_name: {row.get('property_name')}")
            print(f"    unit:          {row.get('unit')}")
            print(f"    rent:          {row.get('rent')}")
            print(f"    past_due:      {row.get('past_due')}")
            print(f"    status:        {row.get('status')}")
            # Also check additional_tenants
            addl = row.get("additional_tenants", "")
            if addl:
                print(f"    addl_tenants:  {addl}")
 
# ═══════════════════════════════════════════════════════════════════════════
# PART C — state.json: check for phantom commitments
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 70}")
print("PART C: STATE.JSON — COMMITMENT AUDIT")
print("=" * 70)
 
state_path = Path("state.json")
if not state_path.exists():
    print("state.json not found — run this script from the repo root")
else:
    state = json.loads(state_path.read_text())
    commitments = state.get("_commitments", {})
 
    this_month = today.strftime("%Y-%m")
    print(f"\nTotal commitment entries: {len(commitments)}")
    print(f"Current month: {this_month}")
 
    # Find ALL commitments whose covers_rent_month == current month
    print(f"\n{'─' * 50}")
    print(f"COMMITMENTS covering {this_month} (would suppress kickstart):")
    print(f"{'─' * 50}")
    found_any = False
    for oid, comms in commitments.items():
        for c in comms:
            if c.get("covers_rent_month") == this_month:
                found_any = True
                print(f"\n  ★ oid={oid}")
                print(f"    event_id:          {(c.get('event_id') or '?')[:20]}...")
                print(f"    anchor_date:       {c.get('anchor_date') or '?'}")
                print(f"    source_type:       {c.get('source_type') or '?'}")
                print(f"    origin_month:      {c.get('origin_month') or '?'}")
                print(f"    covers_rent_month: {c.get('covers_rent_month') or '?'}")
                print(f"    calendar_id:       {(c.get('calendar_id') or 'NOT SET')[:30]}...")
    if not found_any:
        print("  (none — suppress_kickstart should not fire for any tenant)")
 
    # List ALL commitments for easy inspection
    print(f"\n{'─' * 50}")
    print("ALL COMMITMENTS (full dump):")
    print(f"{'─' * 50}")
    for oid, comms in sorted(commitments.items()):
        for c in comms:
            anchor = c.get('anchor_date') or '?'
            src    = c.get('source_type') or '?'
            covers = c.get('covers_rent_month') or '—'
            origin = c.get('origin_month') or '?'
            print(f"  oid={oid:20s}  anchor={anchor:12s}  "
                  f"src={src:10s}  covers={covers:8s}  origin={origin:8s}")
 
    # Also check for state entries where status_event_id is None for current month
    print(f"\n{'─' * 50}")
    print(f"STATE ENTRIES for {this_month} with NO status_event_id:")
    print(f"{'─' * 50}")
    found_missing = False
    for key, entry in sorted(state.items()):
        if key.startswith("_"):
            continue
        if key.endswith(f"_{this_month}"):
            sid = entry.get("status_event_id")
            rid = entry.get("rent_event_id")
            if not sid and not rid:
                found_missing = True
                print(f"  ★ {key}: NO status_event_id, NO rent_event_id")
                print(f"    status={entry.get('status')}  past_due={entry.get('past_due')}")
    if not found_missing:
        print("  (all current-month entries have an event ID — state looks OK)")
 
print("\n\nDone. Share the output and I can pinpoint the exact fix.")