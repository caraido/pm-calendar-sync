"""
inspect_advance_payments.py
────────────────────────────
Probe how AppFolio handles advance/prepayments in the tenant_ledger.

Strategy:
1. Pull full ledger for a wide date range
2. Find credit rows where payment date is BEFORE the 1st of that month
   (these are likely advance payments)
3. Inspect their descriptions and amounts to understand AppFolio's behavior
4. Also look for "prepay", "advance", "June rent", "next month" keywords
   in descriptions — tenants sometimes annotate what a payment is for

Usage (Windows):
    set APPFOLIO_DB_NAME=openkey
    set APPFOLIO_CLIENT_ID=xxx
    set APPFOLIO_CLIENT_SECRET=xxx
    python inspect_advance_payments.py
"""

import json, requests
from datetime import date

from local_config import get_config

DB  = get_config("APPFOLIO_DB_NAME")
CID = get_config("APPFOLIO_CLIENT_ID")
CSC = get_config("APPFOLIO_CLIENT_SECRET")
BASE    = f"https://{CID}:{CSC}@{DB}.appfolio.com/api/v2/reports"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Pull 6 months back to catch any prepayment patterns
today      = date.today()
from_date  = date(today.year - 1 if today.month < 6 else today.year,
                  (today.month - 5) % 12 or 12, 1).isoformat()
to_date    = today.isoformat()

print(f"Pulling ledger from {from_date} to {to_date}...\n")

r = requests.post(
    f"{BASE}/tenant_ledger.json",
    headers=HEADERS,
    json={"from_date": from_date, "to_date": to_date},
    timeout=30,
)

if r.status_code != 200:
    print(f"❌ {r.status_code}: {r.text[:200]}")
    exit(1)

rows = r.json().get("results", [])
credits = [row for row in rows if row.get("credit") not in (None, "", 0)]
print(f"Total rows: {len(rows)}")
print(f"Credit rows (payments): {len(credits)}")

# ── Find payments made BEFORE the 1st of that payment's month ────────────────
print("\n" + "="*60)
print("ADVANCE PAYMENTS (paid before 1st of the month)")
print("="*60)
advance = []
for row in credits:
    try:
        d = date.fromisoformat(row["date"])
        if d.day < 1:  # impossible, just in case
            continue
        # A payment on the 28th-31st could be for next month
        if d.day >= 7:
            advance.append(row)
    except (ValueError, TypeError):
        continue

if advance:
    print(f"Found {len(advance)} payments on days 25-31 (possible advance payments):\n")
    for row in advance:
        print(json.dumps(row, indent=4, default=str))
else:
    print("None found — no payments on days 25-31 in this dataset.")

# ── Look for "next month", "advance", "prepay", future month names ────────────
print("\n" + "="*60)
print("KEYWORD SEARCH in payment descriptions")
print("="*60)

ADVANCE_KEYWORDS = [
    "advance", "prepay", "pre-pay", "next month",
    "june", "july", "august", "september",  # future month names
    "april",  # if found in May — was for last month, if in ledger now
]

keyword_hits = []
for row in credits:
    desc = (row.get("description") or "").lower()
    for kw in ADVANCE_KEYWORDS:
        if kw in desc:
            keyword_hits.append((kw, row))
            break

if keyword_hits:
    print(f"Found {len(keyword_hits)} payments with advance-related keywords:\n")
    for kw, row in keyword_hits:
        print(f"  Keyword matched: '{kw}'")
        print(json.dumps(row, indent=4, default=str))
        print()
else:
    print("No advance-related keywords found in payment descriptions.")

# ── Check credit_debit_balance field ─────────────────────────────────────────
print("\n" + "="*60)
print("RUNNING BALANCE — checking credit_debit_balance field")
print("="*60)

non_null_balance = [
    row for row in rows
    if row.get("credit_debit_balance") not in (None, "")
]
if non_null_balance:
    print(f"✅ credit_debit_balance is populated in {len(non_null_balance)} rows!")
    print("First 5 examples:")
    for row in non_null_balance[:5]:
        print(json.dumps(row, indent=4, default=str))
else:
    print("❌ credit_debit_balance is null in all rows — running balance unavailable.")

# ── Show all unique description patterns for credits ─────────────────────────
print("\n" + "="*60)
print("ALL UNIQUE CREDIT DESCRIPTIONS (full list)")
print("="*60)
descs = sorted(set(r.get("description", "") for r in credits))
for d in descs:
    print(f"  {repr(d)}")

print("\nDone.")