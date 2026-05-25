"""
inspect_payments.py
────────────────────
Deep probe for per-tenant payment records.

1. Pull full tenant_ledger, isolate CREDIT rows (actual payments),
   check if description identifies the tenant/unit.
2. Try payment-specific reports.
3. Try cash_flow and other financial reports that might have payment detail.

Usage (Windows):
    set APPFOLIO_DB_NAME=openkey
    set APPFOLIO_CLIENT_ID=xxx
    set APPFOLIO_CLIENT_SECRET=xxx
    python inspect_payments.py
"""

import os, json, requests
from datetime import date

DB  = os.environ["APPFOLIO_DB_NAME"]
CID = os.environ["APPFOLIO_CLIENT_ID"]
CSC = os.environ["APPFOLIO_CLIENT_SECRET"]
BASE    = f"https://{CID}:{CSC}@{DB}.appfolio.com/api/v2/reports"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

today       = date.today()
month_start = today.replace(day=1).isoformat()
month_end   = today.isoformat()
year_start  = "2026-01-01"


def post(report, payload, label=None):
    url = f"{BASE}/{report}.json"
    r   = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    tag = label or report
    print(f"\n{'='*60}")
    print(f"POST {tag}  →  HTTP {r.status_code}")
    print('='*60)
    if r.status_code == 200:
        body    = r.json()
        results = body.get("results", body)
        if isinstance(results, list):
            return results
        return []
    elif r.status_code == 429:
        print("⏳  Rate limited")
    elif r.status_code == 400:
        print(f"❌  {r.text[:150]}")
    else:
        print(f"⚠️  {r.status_code}: {r.text[:150]}")
    return None


# ── PART 1: Full tenant_ledger — isolate CREDIT rows ────────────────────────
print("\n" + "#"*60)
print("PART 1 — tenant_ledger: all CREDIT rows (actual payments)")
print("Checking if description identifies the tenant/unit")
print("#"*60)

rows = post("tenant_ledger",
            {"from_date": year_start, "to_date": month_end},
            "tenant_ledger full year")

if rows is not None:
    print(f"Total rows: {len(rows)}")
    credits = [r for r in rows if r.get("credit") not in (None, "", "0.00", 0)]
    print(f"Credit rows (payments): {len(credits)}")
    print(f"\nFirst 10 payment rows:")
    for i, row in enumerate(credits[:10]):
        print(f"\n  Payment {i+1}: {json.dumps(row, indent=4, default=str)}")

    # Show unique description patterns in credit rows
    descs = sorted(set(r.get("description", "") for r in credits))
    print(f"\nUnique description values in credit rows ({len(descs)} total):")
    for d in descs[:20]:
        print(f"  {repr(d)}")


# ── PART 2: Payment-specific reports ────────────────────────────────────────
print("\n" + "#"*60)
print("PART 2 — Payment-specific report names")
print("#"*60)

PAYMENT_REPORTS = [
    ("cash_receipts",          {"from_date": month_start, "to_date": month_end}),
    ("payment_journal",        {"from_date": month_start, "to_date": month_end}),
    ("receipts_journal",       {"from_date": month_start, "to_date": month_end}),
    ("tenant_payment_history", {"from_date": month_start, "to_date": month_end}),
    ("deposit_detail",         {"from_date": month_start, "to_date": month_end}),
    ("bank_deposit",           {"from_date": month_start, "to_date": month_end}),
    ("bank_deposits",          {"from_date": month_start, "to_date": month_end}),
    ("receipt_detail",         {"from_date": month_start, "to_date": month_end}),
    ("receipts",               {"from_date": month_start, "to_date": month_end}),
    ("payment_detail",         {"from_date": month_start, "to_date": month_end}),
    ("tenant_receipts",        {"from_date": month_start, "to_date": month_end}),
    ("cash_flow",              {"from_date": month_start, "to_date": month_end}),
    ("receivable_aging",       {}),
    ("delinquency",            {}),
]

for name, payload in PAYMENT_REPORTS:
    rows = post(name, payload)
    if rows is not None:
        print(f"✅  {len(rows)} rows  |  Keys: {list(rows[0].keys()) if rows else 'empty'}")
        for i, row in enumerate(rows[:3]):
            print(f"  Row {i+1}: {json.dumps(row, indent=4, default=str)}")

print("\n\nDone. Paste output here.")