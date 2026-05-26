"""
inspect_latasha.py
──────────────────
Check Latasha Hawkins' current past_due in rent_roll, and look for
any tenants with negative past_due (credit balance / prepayment).

This tells us whether AppFolio represents advance payments as
negative past_due or just shows $0.

Usage:
    set APPFOLIO_DB_NAME=openkey
    set APPFOLIO_CLIENT_ID=xxx
    set APPFOLIO_CLIENT_SECRET=xxx
    python inspect_latasha.py
"""

import os, json, requests
from datetime import date

DB  = os.environ["APPFOLIO_DB_NAME"]
CID = os.environ["APPFOLIO_CLIENT_ID"]
CSC = os.environ["APPFOLIO_CLIENT_SECRET"]
BASE    = f"https://{CID}:{CSC}@{DB}.appfolio.com/api/v2/reports"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

r = requests.post(f"{BASE}/rent_roll.json", headers=HEADERS, json={}, timeout=30)
rows = r.json().get("results", [])

print(f"Total rent_roll rows: {len(rows)}\n")

# ── 1. Find Latasha specifically ─────────────────────────────────────────────
print("="*60)
print("LATASHA HAWKINS — rent_roll row")
print("="*60)
latasha_rows = [
    row for row in rows
    if "latasha" in (row.get("tenant") or "").lower()
    or "hawkins" in (row.get("tenant") or "").lower()
]
if latasha_rows:
    for row in latasha_rows:
        print(json.dumps({
            "tenant":      row.get("tenant"),
            "unit":        row.get("unit"),
            "property":    row.get("property_name"),
            "rent":        row.get("rent"),
            "past_due":    row.get("past_due"),
            "status":      row.get("status"),
            "occupancy_id": row.get("occupancy_id"),
        }, indent=4))
else:
    print("Not found — check name spelling")

# ── 2. Check ALL past_due values: any negative? ───────────────────────────────
print("\n" + "="*60)
print("ALL TENANTS — past_due summary")
print("="*60)

zero     = [r for r in rows if float(r.get("past_due") or 0) == 0]
positive = [r for r in rows if float(r.get("past_due") or 0) > 0]
negative = [r for r in rows if float(r.get("past_due") or 0) < 0]

print(f"past_due == 0    (fully paid / current):  {len(zero)}")
print(f"past_due >  0    (owes money):             {len(positive)}")
print(f"past_due <  0    (credit balance / prepaid): {len(negative)}")

if negative:
    print("\nTenants with NEGATIVE past_due (have a credit balance):")
    for row in negative:
        print(f"  {row.get('tenant'):30}  past_due={row.get('past_due'):>10}  "
              f"rent={row.get('rent'):>8}  unit={row.get('unit')}  "
              f"property={row.get('property_name')}")
else:
    print("\n→ No negative past_due values found.")
    print("  AppFolio likely floors past_due at $0 even when tenant has a credit.")
    print("  This means we CANNOT distinguish 'paid in full' from 'paid in advance'")
    print("  using past_due alone.")