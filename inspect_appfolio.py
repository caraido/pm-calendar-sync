"""
inspect_appfolio_round3.py
──────────────────────────
Probe for:
  - Tenant phone numbers (#4)
  - Per-occupancy payment history for current month (#5)

Usage (Windows):
    set APPFOLIO_DB_NAME=openkey
    set APPFOLIO_CLIENT_ID=xxx
    set APPFOLIO_CLIENT_SECRET=xxx
    python inspect_appfolio_round3.py
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

# occupancy_id=101 from first rent_roll row — used to test per-occupancy filters
TEST_OCCUPANCY_ID = 101

def probe(report, payload, label=None):
    url = f"{BASE}/{report}.json"
    r   = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    tag = label or report
    print(f"\n{'='*60}")
    print(f"POST {tag}  →  HTTP {r.status_code}")
    print(f"Payload: {json.dumps(payload)}")
    print('='*60)
    if r.status_code == 200:
        body    = r.json()
        results = body.get("results", body)
        if isinstance(results, list) and results:
            print(f"✅  {len(results)} rows  |  Keys: {list(results[0].keys())}")
            # Print first 3 rows to see data shape
            for i, row in enumerate(results[:3]):
                print(f"\n  Row {i+1}:", json.dumps(row, indent=4, default=str))
        else:
            print("✅  200 OK — empty or unexpected shape")
            print(json.dumps(body, indent=4, default=str)[:600])
    elif r.status_code == 400:
        print(f"❌  {r.text[:200]}")
    elif r.status_code == 404:
        print("❌  Not found")
    elif r.status_code == 429:
        print("⏳  Rate limited — wait and retry")
    else:
        print(f"⚠️  {r.status_code}: {r.text[:200]}")


print("\n" + "#"*60)
print("PART 1 — Tenant phone numbers")
print("#"*60)

PHONE_REPORTS = [
    ("tenant_directory",  {}),
    ("tenant_contact",    {}),
    ("tenant_list",       {}),
    ("tenant_info",       {}),
    ("tenants",           {}),
    ("tenant_detail",     {}),
]
for name, payload in PHONE_REPORTS:
    probe(name, payload)


print("\n" + "#"*60)
print("PART 2 — tenant_ledger filtered by occupancy_id (payment history)")
print("#"*60)

# Try every plausible filter key for occupancy
LEDGER_ATTEMPTS = [
    {"occupancy_id": TEST_OCCUPANCY_ID,
     "from_date": month_start, "to_date": month_end},
    {"occupancy_ids": [TEST_OCCUPANCY_ID],
     "from_date": month_start, "to_date": month_end},
    {"id": TEST_OCCUPANCY_ID,
     "from_date": month_start, "to_date": month_end},
    {"tenant_id": TEST_OCCUPANCY_ID,
     "from_date": month_start, "to_date": month_end},
    # Try without date range — maybe it requires no dates
    {"occupancy_id": TEST_OCCUPANCY_ID},
    # Try full year to make sure there's data
    {"occupancy_id": TEST_OCCUPANCY_ID,
     "from_date": "2026-01-01", "to_date": month_end},
]
for payload in LEDGER_ATTEMPTS:
    probe("tenant_ledger", payload,
          label=f"tenant_ledger {list(payload.keys())}")


print("\n" + "#"*60)
print("PART 3 — Unfiltered tenant_ledger (see if occupancy info is in rows)")
print("#"*60)
# Pull unfiltered for current month — check if any row has occupancy_id
probe("tenant_ledger", {"from_date": month_start, "to_date": month_end},
      label="tenant_ledger unfiltered this month")

print("\n\nDone. Paste output here.")